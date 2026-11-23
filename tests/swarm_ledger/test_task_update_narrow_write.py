import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.swarm_ledger import api_service
from scripts.swarm_ledger.api import mutations, resources
from scripts.swarm_ledger.events.publishing import Publishing
from scripts.swarm_ledger.repository.entry import Entry
from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository
from tests.swarm_ledger import test_api_v1
from tests.swarm_ledger.narrow_write_cases import (
    CLAIMANT,
    FALLBACKS,
    FEATURE,
    SCENARIOS,
    WORKSPACE,
    assert_falls_back,
    assert_same_write,
    claim_op,
    rows_of,
    twins,
)
from tests.swarm_ledger.test_api_v1 import SLUG, request

authority_live = test_api_v1.authority_live
live = test_api_v1.live

LARGE = "large"


def operate(live: dict, op: dict, guards: dict | None = None) -> tuple[int, dict]:
    return request(live, "POST", "operations", {"ops": [{"id": uuid.uuid4().hex, **op}], "guards": guards or {}})


def add_task(live: dict, task: str, **fields: Any) -> None:
    op = {"op": "task_add", "by": "swarm", "task": task, "title": f"Claim proof {task}", "lane": "eng", **fields}
    status, reply = operate(live, op)
    assert status == 200, reply


def hub_claim(live: dict, task: str, claimed_by: str = "", generation: int = 0) -> tuple[int, dict]:
    revision = request(live, "GET", f"tasks/{task}")[1]["revision"]
    op = {
        "op": "task_update",
        "by": "swarm",
        "item": f"tasks/{task}",
        "fields": {"state": "claimed", "claimed_by": CLAIMANT, "workspace": WORKSPACE},
        "if_state": ["open"],
        "if_match": {"claimed_by": claimed_by, "claim_generation": generation},
    }
    return operate(live, op, {f"tasks/{task}": revision})


def row(live: dict, task: str) -> dict:
    return request(live, "GET", f"tasks/{task}")[1]["data"]


def exported(live: dict) -> dict:
    return request(live, "POST", "export", {"since": 0})[1]["data"]


@pytest.mark.feature(FEATURE)
def test_a_hub_claim_through_the_api_bumps_the_claim_generation_and_records_the_claim(live: dict) -> None:
    add_task(live, "t1")
    status, reply = hub_claim(live, "t1")
    assert status == 200, reply
    assert reply["applied"] and reply["rejected"] == []
    claimed = row(live, "t1")
    assert (claimed["state"], claimed["claimed_by"], claimed["claim_generation"]) == ("claimed", CLAIMANT, 1)
    assert claimed["workspace"] == WORKSPACE
    meta = exported(live)["_meta"]
    assert {"by": "swarm", "kind": "task claimed", "target": "tasks/t1"}.items() <= meta["events"][-1].items()
    assert meta["events"][-1]["rev"] == reply["_meta"]["rev"] == meta["rev"]
    for field in ("state", "claimed_by", "workspace", "claim_generation"):
        assert meta["stamps"][f"tasks/t1/{field}"] == {"at": meta["updated_at"], "rev": meta["rev"], "by": "swarm"}


@pytest.mark.feature(FEATURE)
def test_a_hub_claim_whose_guard_misses_leaves_the_task_as_it_was(live: dict) -> None:
    add_task(live, "t1")
    assert hub_claim(live, "t1")[0] == 200
    before = row(live, "t1")
    status, reply = hub_claim(live, "t1")
    assert status == 200, reply
    assert reply["rejected"] == []
    assert reply["tasks"][0]["claimed_by"] == CLAIMANT
    assert row(live, "t1") == before
    status, reply = hub_claim(live, "t1", claimed_by="engineer@abc123-0002", generation=1)
    assert status == 200 and row(live, "t1") == before


@pytest.mark.feature(FEATURE)
def test_a_hub_claim_on_a_task_with_an_open_dependency_keeps_it_open(live: dict) -> None:
    add_task(live, "t1")
    add_task(live, "t2", depends_on=["t1"])
    status, reply = hub_claim(live, "t2")
    assert status == 200, reply
    held = row(live, "t2")
    assert (held["state"], held["claimed_by"], held.get("claim_generation", 0)) == ("open", "", 0)


@pytest.mark.feature(FEATURE)
def test_a_hub_claim_relays_doctors_and_delivers_alerts_at_its_revision(live: dict, monkeypatch) -> None:
    add_task(live, "t1")
    seen = []
    for name in ("relay_to_inbox", "doctor_phrase", "deliver_alerts"):
        monkeypatch.setattr(api_service, name, lambda slug, state, name=name: seen.append((name, slug, state)))
    status, reply = hub_claim(live, "t1")
    assert status == 200, reply
    assert [name for name, _, _ in seen] == ["relay_to_inbox", "doctor_phrase", "deliver_alerts"]
    for _, slug, state in seen:
        assert slug == SLUG
        assert state["_meta"]["rev"] == reply["_meta"]["rev"]
        assert state["_meta"]["events"][-1]["kind"] == "task claimed"
        assert isinstance(state["alerts"], list)


@pytest.mark.feature(FEATURE)
def test_a_refused_hub_claim_raises_its_alert_for_delivery(live: dict, monkeypatch) -> None:
    from scripts.swarm_ledger import ledger_server as server

    add_task(live, "t1")
    document = server.repository.export_document(SLUG)
    document["_meta"]["outcomes"] = {"t1": {"state": "done"}}
    server.repository.import_document(SLUG, document, replace=True)
    delivered = []
    monkeypatch.setattr(api_service, "deliver_alerts", lambda slug, state: delivered.append(state))
    status, reply = hub_claim(live, "t1")
    assert status == 200, reply
    assert reply["rejected"] and reply["applied"] == []
    raised = [a for a in delivered[0]["alerts"] if a["rev"] == reply["_meta"]["rev"] and a["state"] == "open"]
    assert [alert["item"] for alert in raised] == ["tasks/t1"]
    assert "committed outcomes require controller reconciliation" in raised[0]["text"]
    assert row(live, "t1")["state"] == "open"


@pytest.mark.feature(FEATURE)
def test_a_hub_claim_reply_carries_the_projected_row_and_the_new_revision(live: dict) -> None:
    add_task(live, "t1")
    status, reply = hub_claim(live, "t1")
    assert status == 200, reply
    assert set(reply) == {"tasks", "applied", "rejected", "_meta"}
    stored = next(task for task in exported(live)["tasks"] if task["id"] == "t1")
    assert reply["tasks"] == [resources.project(stored)]
    assert reply["_meta"]["rev"] == request(live, "GET", "metadata")[1]["data"]["_meta"]["rev"]


def services(repository: Any, calls: list) -> Any:
    def hook(name: str) -> Any:
        return lambda slug, state: calls.append((name, slug, state))

    gate = SimpleNamespace(apply=lambda doc, op, ctx, apply_op: apply_op(doc, op, ctx), publish=lambda rejected: None)
    authority = SimpleNamespace(refusal=lambda principal, op: None, fence=lambda slug, epoch: None)
    ledger = SimpleNamespace(
        core=repository.domain,
        authority=authority,
        write_gate=lambda slug: gate,
        relay_to_inbox=hook("relay"),
        deliver_alerts=hook("alerts"),
    )
    media = SimpleNamespace(resolve_media=lambda slug, ops: ops, resolve_artifacts=lambda slug, ops: ops)
    return SimpleNamespace(
        repository=repository, ledger=ledger, swarm=SimpleNamespace(doctor_phrase=hook("doctor")), media=media
    )


def whole_read(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("a hub claim read the whole ledger")


def large(tmp_path: Any, tasks: int = 1000) -> SQLiteLedgerRepository:
    repository = SQLiteLedgerRepository(tmp_path / "large.sqlite3")
    content = {
        "title": "Large",
        "phases": [{"title": "Proof", "description": "d"}],
        "tasks": [
            {"title": f"task {index}", "description": "words " * 200, "phase": "p1", "lane": "eng"}
            for index in range(tasks)
        ],
    }
    assert repository.create(LARGE, content, size="swarm")
    return repository


@pytest.mark.feature(FEATURE)
def test_a_hub_claim_on_a_large_ledger_never_reads_or_returns_the_whole_ledger(tmp_path, monkeypatch) -> None:
    repository = large(tmp_path)
    revision = resources.resource_revision(repository.read(LARGE, "tasks/t500"), "tasks/t500")
    for owner, name in ((SQLiteLedgerRepository, "current"), (SQLiteLedgerRepository, "get_document")):
        monkeypatch.setattr(owner, name, whole_read)
    monkeypatch.setattr(SQLiteLedgerRepository, "_entry", whole_read)
    monkeypatch.setattr(Entry, "for_write", whole_read)
    calls: list = []
    payload = {"operation_id": uuid.uuid4().hex, "ops": [claim_op("t500")], "guards": {"tasks/t500": revision}}
    reply = mutations.apply(services(repository, calls), LARGE, "", payload)
    assert reply["applied"] == ["claim"]
    assert [(r["id"], r["claimed_by"], r["claim_generation"]) for r in reply["tasks"]] == [("t500", CLAIMANT, 1)]
    assert len(json.dumps(reply)) < 4000
    assert [name for name, _, _ in calls] == ["relay", "doctor", "alerts"]
    assert all([task["id"] for task in state["tasks"]] == ["t500"] for _, _, state in calls)
    monkeypatch.undo()
    stored = SQLiteLedgerRepository(repository.path).get_document(LARGE)
    claimed = next(task for task in stored["tasks"] if task["id"] == "t500")
    assert (claimed["claimed_by"], claimed["claim_generation"]) == (CLAIMANT, 1)
    assert stored["_meta"]["rev"] == reply["_meta"]["rev"]


@pytest.mark.feature(FEATURE)
@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_a_narrow_claim_stores_what_the_whole_write_stores(tmp_path, monkeypatch, scenario: str) -> None:
    def make(name: str) -> SQLiteLedgerRepository:
        return SQLiteLedgerRepository(tmp_path / name / "ledger.sqlite3")

    assert_same_write(make, scenario, monkeypatch)


def guarded(repository: Any, slug: str, op: dict, guards: bool = True) -> mutations.GuardedOperations:
    revision = resources.resource_revision(repository.read(slug, op["item"]), op["item"])
    payload = {"operation_id": "o1", "ops": [op], "guards": {op["item"]: revision} if guards else {}}
    budget = SimpleNamespace(apply=lambda doc, op, ctx, apply_op: apply_op(doc, op, ctx))
    return mutations.GuardedOperations(payload, budget, repository.domain, lambda epoch: None, slug)


@pytest.mark.feature(FEATURE)
def test_a_guarded_narrow_claim_and_its_replay_then_a_whole_write_match_the_whole_path(tmp_path, monkeypatch) -> None:
    def make(name: str) -> SQLiteLedgerRepository:
        return SQLiteLedgerRepository(tmp_path / name / "ledger.sqlite3")

    whole, narrow, left, right, clock = twins(make, None, monkeypatch)
    comment = {"op": "add", "id": "note", "thread": "tasks/t1/comments", "text": "a comment", "by": "swarm"}
    whole.apply_ops(left, ops=[comment])
    narrow.apply_ops(right, ops=[comment])
    clock.tick()
    op = claim_op()
    whole.apply_ops(left, ops=[op], gate=guarded(whole, left, op))
    assert narrow.apply_task_update(right, op, guarded(narrow, right, op)) is not None
    clock.tick()
    whole.apply_ops(left, ops=[op], gate=guarded(whole, left, op, guards=False))
    assert narrow.apply_task_update(right, op, guarded(narrow, right, op, guards=False)) is not None
    clock.tick()
    after = {"op": "add", "id": "after", "thread": "chat", "text": "after"}
    whole.apply_ops(left, ops=[after])
    narrow.apply_ops(right, ops=[after])
    assert rows_of(narrow, right) == rows_of(whole, left)
    assert narrow.get_document(right) == make("narrow").get_document(right) == whole.get_document(left)


@pytest.mark.feature(FEATURE)
@pytest.mark.parametrize("prepare", FALLBACKS)
def test_a_claim_the_narrow_write_cannot_prove_falls_back_with_nothing_written(tmp_path, monkeypatch, prepare) -> None:
    assert_falls_back(lambda name: SQLiteLedgerRepository(tmp_path / name / "ledger.sqlite3"), prepare, monkeypatch)


@pytest.mark.feature(FEATURE)
def test_the_narrow_write_returns_only_the_touched_row_and_the_new_revision(tmp_path) -> None:
    repository = large(tmp_path, tasks=50)
    written = repository.apply_task_update(LARGE, claim_op("t7"))
    assert written is not None and written.rejected == []
    assert (written.task["id"], written.task["claimed_by"]) == ("t7", CLAIMANT)
    assert [task["id"] for task in written.state["tasks"]] == ["t7"]
    assert "phases" not in written.state and "chat" not in written.state
    assert written.state["_meta"]["rev"] == repository.read(LARGE, "_meta.rev")["_meta"]["rev"]


@pytest.mark.feature(FEATURE)
def test_only_a_single_guarded_hub_claim_takes_the_narrow_path() -> None:
    hub = {"operation_id": "o", "ops": [claim_op()], "guards": {}}
    assert mutations.claim(hub) == claim_op()
    assert mutations.claim({**hub, "ops": [claim_op(controller_epoch=3)]}) is not None
    refused = [
        {**hub, "ops": [claim_op(), claim_op(id="second")]},
        {**hub, "changes": [{"path": "tasks/t1/done", "value": True}]},
        {**hub, "ops": [claim_op(worker_claim={"agent": "a"})]},
        {**hub, "ops": [claim_op(by="engineer@abc123-0001")]},
        {**hub, "ops": [{key: value for key, value in claim_op().items() if key not in ("if_state", "if_match")}]},
        {**hub, "ops": [{**claim_op(), "fields": {"state": "claimed", "claimed_by": CLAIMANT, "kind": "code"}}]},
        {**hub, "ops": [{**claim_op(), "fields": {"state": "done"}}]},
        {**hub, "ops": [{"op": "task_add", "id": "a", "by": "swarm", "task": "t9", "title": "x", "lane": "eng"}]},
    ]
    assert [mutations.claim(payload) for payload in refused] == [None] * len(refused)


@pytest.mark.feature(FEATURE)
@pytest.mark.parametrize(("published", "written", "reads"), [(True, "row", 1), (False, "row", 0), (True, None, 0)])
def test_publishing_refreshes_only_a_published_ledger_after_a_narrow_write(published, written, reads) -> None:
    seen = []
    inner = SimpleNamespace(
        apply_task_update=lambda slug, op, gate: written,
        current=lambda slug: seen.append(slug) or SimpleNamespace(generation=-1),
    )
    wrapper = Publishing(inner)
    if published:
        wrapper.published["demo"] = 0
    assert wrapper.apply_task_update("demo", claim_op()) == written
    assert seen == ["demo"] * reads
