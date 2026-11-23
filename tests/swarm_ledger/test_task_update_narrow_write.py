import json
import threading
import uuid
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.swarm_ledger import api_service, request_metrics
from scripts.swarm_ledger.api import mutations, resources
from scripts.swarm_ledger.api.errors import APIError
from scripts.swarm_ledger.events.publishing import Publishing
from scripts.swarm_ledger.repository import legacy, task_write
from scripts.swarm_ledger.repository.entry import Entry
from scripts.swarm_ledger.repository.rows import encode
from scripts.swarm_ledger.repository.sqlite import Missing, SQLiteLedgerRepository
from tests.swarm_ledger import test_api_v1
from tests.swarm_ledger.narrow_write_cases import (
    CLAIMANT,
    FALLBACKS,
    FREED,
    SCENARIOS,
    SETTLE,
    TABLES,
    WORKSPACE,
    assert_falls_back,
    assert_same,
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


@pytest.mark.feature("ledger-task-update-narrow-write")
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


@pytest.mark.feature("ledger-task-update-narrow-write")
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


@pytest.mark.feature("ledger-task-update-narrow-write")
def test_a_hub_claim_on_a_task_with_an_open_dependency_keeps_it_open(live: dict) -> None:
    add_task(live, "t1")
    add_task(live, "t2", depends_on=["t1"])
    status, reply = hub_claim(live, "t2")
    assert status == 200, reply
    held = row(live, "t2")
    assert (held["state"], held["claimed_by"], held.get("claim_generation", 0)) == ("open", "", 0)


@pytest.mark.feature("ledger-task-update-narrow-write")
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


@pytest.mark.feature("ledger-task-update-narrow-write")
def test_a_refused_hub_claim_raises_its_alert_for_delivery(live: dict, monkeypatch) -> None:
    from scripts.swarm_ledger import ledger_server as server

    add_task(live, "t1")
    document = server.repository.export_document(SLUG)
    document["_meta"]["outcomes"] = {"t1": {"state": "done"}}
    server.repository.import_document(SLUG, document, token=server.repository.token(SLUG), replace=True)
    delivered = []
    monkeypatch.setattr(api_service, "deliver_alerts", lambda slug, state: delivered.append(state))
    status, reply = hub_claim(live, "t1")
    assert status == 200, reply
    assert reply["rejected"] and reply["applied"] == []
    raised = [a for a in delivered[0]["alerts"] if a["rev"] == reply["_meta"]["rev"] and a["state"] == "open"]
    assert [alert["item"] for alert in raised] == ["tasks/t1"]
    assert "committed outcomes require controller reconciliation" in raised[0]["text"]
    assert row(live, "t1")["state"] == "open"


@pytest.mark.feature("ledger-task-update-narrow-write")
def test_a_hub_claim_reply_carries_the_projected_row_and_the_new_revision(live: dict) -> None:
    add_task(live, "t1")
    status, reply = hub_claim(live, "t1")
    assert status == 200, reply
    assert set(reply) == {"tasks", "applied", "rejected", "_meta"}
    stored = next(task for task in exported(live)["tasks"] if task["id"] == "t1")
    assert reply["tasks"] == [resources.project(stored)]
    assert reply["_meta"]["rev"] == request(live, "GET", "metadata")[1]["data"]["_meta"]["rev"]


def services(repository: Any, calls: list, slug: str = LARGE, refusal: Any = None) -> Any:
    def hook(name: str) -> Any:
        return lambda slug, state: calls.append((name, slug, state))

    gate = SimpleNamespace(apply=lambda doc, op, ctx, apply_op: apply_op(doc, op, ctx), publish=lambda rejected: None)
    authority = SimpleNamespace(refusal=refusal or (lambda principal, op: None), fence=lambda slug, epoch: None)
    ledger = SimpleNamespace(
        core=repository.domain,
        authority=authority,
        write_gate=lambda name: gate if name == slug else None,
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


@pytest.mark.feature("ledger-task-update-narrow-write")
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


@pytest.mark.feature("ledger-task-update-narrow-write")
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


@pytest.mark.feature("ledger-task-update-narrow-write")
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


@pytest.mark.feature("ledger-task-update-narrow-write")
@pytest.mark.parametrize("prepare", FALLBACKS)
def test_a_claim_the_narrow_write_cannot_prove_falls_back_with_nothing_written(tmp_path, monkeypatch, prepare) -> None:
    assert_falls_back(lambda name: SQLiteLedgerRepository(tmp_path / name / "ledger.sqlite3"), prepare, monkeypatch)


@pytest.mark.feature("ledger-task-update-narrow-write")
def test_the_narrow_write_returns_only_the_touched_row_and_the_new_revision(tmp_path) -> None:
    repository = large(tmp_path, tasks=50)
    written = repository.apply_task_update(LARGE, claim_op("t7"))
    assert isinstance(written, task_write.Written) and written.rejected == []
    assert (written.task["id"], written.task["claimed_by"]) == ("t7", CLAIMANT)
    assert [task["id"] for task in written.state["tasks"]] == ["t7"]
    assert "phases" not in written.state and "chat" not in written.state
    assert written.state["_meta"]["rev"] == repository.read(LARGE, "_meta.rev")["_meta"]["rev"]


@pytest.mark.feature("ledger-task-update-narrow-write")
def test_only_a_single_guarded_hub_claim_takes_the_narrow_path() -> None:
    hub = {"operation_id": "o", "ops": [claim_op()], "guards": {}}
    assert mutations.claim(hub) == claim_op()
    assert mutations.claim({**hub, "ops": [claim_op(controller_epoch=3)]}) is not None
    one_guard = [
        {key: value for key, value in claim_op().items() if key != "if_match"},
        {key: value for key, value in claim_op().items() if key != "if_state"},
        {**claim_op(), "fields": {"claimed_by": CLAIMANT}},
    ]
    assert [mutations.claim({**hub, "ops": [op]}) for op in one_guard] == one_guard
    refused = [
        {**hub, "ops": [claim_op(), claim_op(id="second")]},
        {**hub, "changes": [{"path": "tasks/t1/done", "value": True}]},
        {**hub, "ops": [claim_op(worker_claim={"agent": "a"})]},
        {**hub, "ops": [claim_op(by="engineer@abc123-0001")]},
        {**hub, "ops": [{key: value for key, value in claim_op().items() if key not in ("if_state", "if_match")}]},
        {**hub, "ops": [{**claim_op(), "fields": {"state": "claimed", "claimed_by": CLAIMANT, "kind": "code"}}]},
        {**hub, "ops": [{**claim_op(), "fields": {"state": "done"}}]},
        {**hub, "ops": [{**claim_op(), "fields": {}}]},
        {**hub, "ops": [{**claim_op(), "fields": ["state"]}]},
        {**hub, "ops": [["op", "id"]]},
        {**hub, "ops": [{"op": "task_add", "id": "a", "by": "swarm", "task": "t9", "title": "x", "lane": "eng"}]},
    ]
    assert [mutations.claim(payload) for payload in refused] == [None] * len(refused)


@pytest.mark.feature("ledger-task-update-narrow-write")
@pytest.mark.parametrize(("published", "written", "reads"), [(True, "row", 1), (False, "row", 0), (True, None, 0)])
def test_publishing_refreshes_only_a_published_ledger_after_a_narrow_write(published, written, reads) -> None:
    seen, gates = [], []
    inner = SimpleNamespace(
        apply_task_update=lambda slug, op, gate: gates.append((slug, gate)) or written,
        current=lambda slug: seen.append((threading.current_thread().name, slug)) or SimpleNamespace(generation=-1),
    )
    wrapper = Publishing(inner)
    if published:
        wrapper.published["demo"] = 0
    assert wrapper.apply_task_update("demo", claim_op(), "gate") == written
    wrapper.drain(5)
    assert gates == [("demo", "gate")]
    assert seen == [("ledger-publish", "demo")] * reads


def made(tmp_path: Any, name: str) -> Callable[[str], SQLiteLedgerRepository]:
    return lambda twin: SQLiteLedgerRepository(tmp_path / name / twin / "ledger.sqlite3")


def done_dependency(document: dict) -> None:
    document["tasks"][0].update(state="done", done=True)
    document["tasks"][1]["depends_on"] = ["t1"]


def parked_on_done(document: dict) -> None:
    document["tasks"][0].update(state="done", done=True)
    document["tasks"][1]["parked_on"] = ["t1"]


def wordy(document: dict) -> None:
    document["overview"] = "word " * 250


CASES = {
    "done dependency": (claim_op("t2"), done_dependency, []),
    "parked on a done task": (claim_op("t2"), parked_on_done, []),
    "stored size warning": (claim_op(), wordy, []),
}


@pytest.mark.feature("ledger-task-update-narrow-write")
@pytest.mark.parametrize("case", sorted(CASES))
def test_a_narrow_claim_reads_its_dependencies_and_keeps_stored_size_warnings(tmp_path, monkeypatch, case) -> None:
    assert_same(made(tmp_path, "same"), CASES[case], monkeypatch)


def refusing(doc: dict, op: dict, ctx: Any, apply_op: Any) -> bool:
    return False


def dirtying(doc: dict, op: dict, ctx: Any, apply_op: Any) -> bool:
    ctx.dirty = True
    return False


@pytest.mark.feature("ledger-task-update-narrow-write")
@pytest.mark.parametrize("refuse", [refusing, dirtying])
def test_a_claim_its_gate_refuses_is_rejected_and_trims_events_as_the_whole_write_does(
    tmp_path, monkeypatch, refuse
) -> None:
    whole, narrow, left, right, clock = twins(made(tmp_path, "gated"), None, monkeypatch)
    for ops in ([claim_op(id="first")], [FREED]):
        whole.apply_ops(left, ops=ops)
        narrow.apply_ops(right, ops=ops)
    clock.tick()
    monkeypatch.setattr(whole.domain, "EVENTS_KEPT", 1)
    monkeypatch.setattr(whole.domain, "EVENTS_WINDOW_MS", 0)
    _, rejected = whole.apply_ops(left, ops=[claim_op()], gate=SimpleNamespace(apply=refuse))
    written = narrow.apply_task_update(right, claim_op(), SimpleNamespace(apply=refuse))
    assert written is not None and written.rejected == rejected == ["claim"]
    assert written.task["claimed_by"] == ""
    assert rows_of(narrow, right) == rows_of(whole, left)


def without_task_revision(repository: SQLiteLedgerRepository, slug: str) -> None:
    document = repository.export_document(slug)
    del document["_meta"]["task_revision"]
    repository.import_document(slug, document, replace=True)


@pytest.mark.feature("ledger-task-update-narrow-write")
def test_a_claim_on_a_ledger_without_a_task_revision_stores_the_previous_revision(tmp_path, monkeypatch) -> None:
    whole, narrow, left, right, clock = twins(made(tmp_path, "unrevised"), None, monkeypatch)
    for repository, slug in ((whole, left), (narrow, right)):
        repository.apply_ops(slug, ops=[claim_op(id="first")])
        without_task_revision(repository, slug)
    clock.tick()
    previous = whole.read(left, "_meta.rev")["_meta"]["rev"]
    whole.apply_ops(left, ops=[claim_op()])
    assert narrow.apply_task_update(right, claim_op()) is not None
    assert rows_of(narrow, right) == rows_of(whole, left)
    assert narrow.read(right, "_meta.task_revision")["_meta"]["task_revision"] == previous


def checked(document: dict) -> None:
    document["tasks"][0]["done"] = True


def settled_task(document: dict) -> None:
    document["tasks"][0]["state"] = "done"


def awaiting(document: dict) -> None:
    document["tasks"][0]["awaiting"] = "approval"


def unchanged(document: dict) -> None:
    return None


SETTLED = {
    "checked done": (checked, claim_op()),
    "done state": (settled_task, claim_op()),
    "approval line": (awaiting, {**claim_op(), "fields": {"state": "pr", "claimed_by": CLAIMANT}}),
    "missing task": (unchanged, claim_op("t9")),
}


@pytest.mark.feature("ledger-task-update-narrow-write")
@pytest.mark.parametrize("case", sorted(SETTLED))
def test_a_claim_on_a_settled_task_or_one_that_changes_a_priority_line_falls_back(tmp_path, monkeypatch, case) -> None:
    prepare, op = SETTLED[case]
    assert_falls_back(made(tmp_path, "settled"), prepare, monkeypatch, op)


@pytest.mark.feature("ledger-task-update-narrow-write")
def test_a_ledger_missing_a_part_the_whole_write_creates_falls_back(tmp_path) -> None:
    repository = large(tmp_path, tasks=3)
    with repository.connect() as connection, connection:
        for table in TABLES:
            connection.execute(f"DELETE FROM {table} WHERE slug=? AND path=?", (LARGE, encode(["notifications"])))
    before = rows_of(repository, LARGE)
    assert repository.apply_task_update(LARGE, claim_op()) is None
    assert rows_of(repository, LARGE) == before


@pytest.mark.feature("ledger-task-update-narrow-write")
def test_a_narrow_claim_on_a_missing_ledger_names_it(tmp_path) -> None:
    repository = large(tmp_path, tasks=3)
    with pytest.raises(Missing, match="absent"):
        repository.apply_task_update("absent", claim_op())


@pytest.mark.feature("ledger-task-update-narrow-write")
def test_a_narrow_claim_adopts_only_its_own_legacy_ledger(tmp_path) -> None:
    repository = large(tmp_path, tasks=3)
    other = repository.directory / "other.json"
    old = {"title": "Old", "overview": "o", "phases": [], "questions": [], "followups": []}
    other.write_text(json.dumps({**old, "_meta": {"rev": 3, "stamps": {}, "events": [], "seeds": {}}}))
    assert repository.apply_task_update(LARGE, claim_op()) is not None
    assert other.exists() and not legacy.stored(repository, "other")


@pytest.mark.feature("ledger-task-update-narrow-write")
def test_a_narrow_claim_locks_and_begins_its_own_ledger_and_times_its_write_steps(tmp_path, monkeypatch) -> None:
    repository = large(tmp_path, tasks=3)
    locked, begin = request_metrics.locked, request_metrics.begin_ledger
    held, begun, admitted = [], [], []
    monkeypatch.setattr(request_metrics, "locked", lambda lock: held.append(lock.slug) or locked(lock))
    monkeypatch.setattr(
        request_metrics,
        "begin_ledger",
        lambda connection, slug, statement: begun.append(slug) or begin(connection, slug, statement),
    )
    monkeypatch.setattr(
        SQLiteLedgerRepository, "admit", lambda self, connection, state: admitted.append((connection, state))
    )
    timing = request_metrics.Timing()
    token = request_metrics.CURRENT.set(timing)
    try:
        written = repository.apply_task_update(LARGE, claim_op())
    finally:
        request_metrics.CURRENT.reset(token)
    assert written is not None
    assert held == begun == [LARGE]
    assert {"apply_locked", "event_append", "outbox_write", "refresh_record"} <= set(timing.steps)
    assert [(connection is not None, state is written.state) for connection, state in admitted] == [(True, True)]


@pytest.mark.feature("ledger-task-update-narrow-write")
def test_a_row_written_outside_the_read_keeps_its_stored_key_and_position(tmp_path) -> None:
    repository = large(tmp_path, tasks=3)
    path = encode(task_write.task_parts("t2"))
    with repository.connect() as connection:
        stored = task_write.stored_at(connection, LARGE, [path])[path]
        table, parent, _, _, kind, value = stored
        after = {path: (table, parent, "moved", 99, kind, value)}
        assert task_write.placed(connection, LARGE, {}, after, {parent}) == {path: stored}
    with pytest.raises(RuntimeError, match=task_write.REPLACED):
        task_write.placed(None, LARGE, {}, after, {path})


def hooked(state: dict, held: dict) -> tuple[dict, list]:
    reads: list = []

    def read(slug: str, *paths: str) -> dict:
        reads.append((slug, *paths))
        return {"tasks": [{"id": path.split("/")[1]} for path in paths], "phases": []}

    hooks: Any = SimpleNamespace(repository=SimpleNamespace(held_revisions=lambda: held, read=read))
    return mutations.hook_state(hooks, "demo", state), reads


@pytest.mark.feature("ledger-task-update-narrow-write")
def test_hook_state_reads_the_rows_operator_events_and_open_alerts_name_since_the_oldest_held_revision() -> None:
    events = [
        {"rev": 8, "by": "operator", "target": "tasks/t5/comments"},
        {"rev": 7, "by": "operator", "target": "tasks/t6/comments"},
        {"rev": 9, "by": "swarm", "target": "tasks/t7"},
        {"rev": 9, "by": "operator", "target": "phases/p1"},
        {"rev": 9, "by": "operator", "target": "tasks/t1"},
    ]
    alerts = [
        {"rev": 8, "state": "open", "item": "tasks/t8"},
        {"rev": 7, "state": "open", "item": "tasks/t9"},
        {"rev": 9, "state": "done", "item": "tasks/t10"},
        {"rev": 9, "state": "open", "item": "chat"},
        {"rev": 9, "state": "open"},
        {"state": "open", "item": "tasks/t11"},
    ]
    state = {"_meta": {"rev": 10, "events": events}, "alerts": alerts, "tasks": [{"id": "t1"}]}
    held = {"inbox": {"demo": [12, 8], "other": [1]}, "alerts": {"other": [2]}}
    found, reads = hooked(state, held)
    assert reads == [("demo", "tasks/t5", "tasks/t8")]
    assert found == {**state, "tasks": [{"id": "t1"}, {"id": "t5"}, {"id": "t8"}]}


@pytest.mark.feature("ledger-task-update-narrow-write")
def test_hook_state_leaves_the_written_state_alone_when_nothing_it_names_is_missing() -> None:
    state = {"_meta": {"rev": 3, "events": []}, "alerts": [{"state": "open", "item": "tasks/t11"}], "tasks": []}
    found, reads = hooked(state, {"inbox": {"demo": [0]}})
    assert reads == [] and found is state


@pytest.mark.feature("ledger-task-update-narrow-write")
def test_stored_warnings_reads_only_the_stored_warning_list() -> None:
    for stored, expected in (({}, []), ({"_meta": {}}, []), ({"_meta": {"warnings": ["w"]}}, ["w"])):
        reads: list = []
        reader = SimpleNamespace(read=lambda slug, *keys, stored=stored: reads.append((slug, *keys)) or stored)
        hooks: Any = SimpleNamespace(repository=reader)
        assert mutations.stored_warnings(hooks, "demo") == expected
        assert reads == [("demo", "_meta.warnings")]


def warned(tmp_path: Any) -> SQLiteLedgerRepository:
    repository = large(tmp_path, tasks=5)
    document = repository.export_document(LARGE)
    wordy(document)
    repository.import_document(LARGE, document, replace=True)
    repository.apply_ops(LARGE, ops=[SETTLE])
    return repository


def claim_payload(repository: SQLiteLedgerRepository, task: str, guarded_by: str | None = None) -> dict:
    item = f"tasks/{guarded_by or task}"
    revision = resources.resource_revision(repository.read(LARGE, item), item)
    return {"operation_id": uuid.uuid4().hex, "ops": [claim_op(task)], "guards": {f"tasks/{task}": revision}}


@pytest.mark.feature("ledger-task-update-narrow-write")
def test_a_refused_hub_claim_answers_with_the_stored_warnings_and_the_refusal(tmp_path) -> None:
    repository = warned(tmp_path)

    def refusal(principal: str, op: dict) -> str | None:
        return f"{principal} may not claim" if principal else None

    payload = claim_payload(repository, "t2")
    with pytest.raises(APIError) as refused:
        mutations.apply(services(repository, [], refusal=refusal), LARGE, "engineer@abc123-0009", payload)
    assert refused.value.status == 403
    warnings = ["overview has 250 words, limit 200", "engineer@abc123-0009 may not claim"]
    assert refused.value.details == {"rejected": ["claim"], "_meta": {"warnings": warnings}}


@pytest.mark.feature("ledger-task-update-narrow-write")
def test_a_hub_claim_with_a_stale_guard_is_refused_by_its_write_gate(tmp_path) -> None:
    repository = large(tmp_path, tasks=5)
    payload = claim_payload(repository, "t2", guarded_by="t3")
    with pytest.raises(APIError) as stale:
        mutations.apply(services(repository, []), LARGE, "", payload)
    assert (stale.value.status, stale.value.code) == (409, "revision_conflict")
    assert row_claimant(repository, "t2") == ""


def row_claimant(repository: SQLiteLedgerRepository, task: str) -> str:
    return repository.read(LARGE, f"tasks/{task}")["tasks"][0]["claimed_by"]


@pytest.mark.feature("ledger-task-update-narrow-write")
def test_a_hub_claim_hands_its_hooks_the_task_an_operator_wrote_since_a_held_revision(tmp_path) -> None:
    repository = large(tmp_path, tasks=5)
    repository.hold_revision("inbox", LARGE, repository.read(LARGE, "_meta.rev")["_meta"]["rev"])
    repository.apply_ops(
        LARGE, ops=[{"op": "add", "id": "c1", "thread": "tasks/t3/comments", "text": "operator words"}]
    )
    calls: list = []
    reply = mutations.apply(services(repository, calls), LARGE, "", claim_payload(repository, "t4"))
    assert reply["applied"] == ["claim"]
    assert [(name, slug, [task["id"] for task in state["tasks"]]) for name, slug, state in calls] == [
        (name, LARGE, ["t4", "t3"]) for name in ("relay", "doctor", "alerts")
    ]
