import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def inputs():
    return json.loads((Path(__file__).parents[1] / "fixtures/swarm_v2/learning-outcomes.json").read_text())


@pytest.fixture
def fixture():
    from scripts.gates.brain import bind

    task = inputs()["task"]
    doc = {"tasks": [task], "_meta": {}}
    events = []
    ctx = SimpleNamespace(
        at=100000, meta=doc["_meta"], dirty=False, refused=[], record=lambda *a, **k: events.append((a, k))
    )
    return doc, task, ctx, bind("work", task, "swarm-brain"), events


def operation(kind, scope, **fields):
    return {"op": kind, "id": "request", "item": "tasks/work", "binding": scope, **fields}


@pytest.mark.parametrize("kind", ["lesson", "decision", "no_new_learning"])
def test_gat05_accepts_evidence_or_explicit_no_new_learning(fixture, kind):
    from scripts.swarm_ledger import ledger_brain

    doc, task, ctx, scope, events = fixture
    learning = inputs()["learning"][kind]
    op = operation("brain_learning", scope, by="engineer", proof=task["proof"], learning=learning)
    ledger_brain.check(op)
    assert ledger_brain.apply(doc, op, ctx)
    assert ledger_brain.completion(task) is None
    record = task["learning_outcomes"][0]
    assert record["learning"] == learning
    assert record["proof"] == task["proof"]
    assert record["by"] == "engineer"
    assert record["binding"] == scope
    assert doc["_meta"]["brain_metrics"]["learning_outcomes_by_kind"] == {kind: 1}
    assert events
    assert ledger_brain.apply(doc, copy.deepcopy(op), ctx)
    assert len(task["learning_outcomes"]) == 1
    assert doc["_meta"]["brain_metrics"]["learning_outcomes_by_kind"] == {kind: 1}


def test_gat05_requires_current_learning_without_replacing_implementation_proof(fixture):
    from scripts.swarm_ledger import ledger_brain, ledger_tasks

    doc, task, _, _, _ = fixture
    assert "learning" in ledger_brain.completion(task)
    assert "learning" in ledger_tasks._done_refusal(doc, {"item": "tasks/work"}, {**task, "state": "done"})
    assert "proof" in ledger_tasks._done_refusal(doc, {"item": "tasks/work"}, {**task, "state": "done", "proof": {}})


@pytest.mark.parametrize(
    "field,value", [("task", "other"), ("generation", 1), ("revision", "old"), ("brain", "personal")]
)
def test_gat05_rejects_stale_or_unrelated_outcomes_without_mutation(fixture, field, value):
    from scripts.swarm_ledger import ledger_brain

    doc, task, ctx, scope, _ = fixture
    op = operation(
        "brain_learning",
        {**scope, field: value},
        by="engineer",
        proof=task["proof"],
        learning={"kind": "no_new_learning", "content": "No novel lesson", "sources": []},
    )
    before = copy.deepcopy(doc)
    assert not ledger_brain.apply(doc, op, ctx)
    assert doc == before
    assert ctx.refused


def test_gat05_emergency_is_scoped_expiring_and_idempotently_audited(fixture):
    from scripts.swarm_ledger import ledger_brain

    doc, task, ctx, scope, _ = fixture
    op = operation("brain_emergency", scope, **inputs()["emergency"])
    ledger_brain.check(op)
    assert ledger_brain.apply(doc, op, ctx)
    assert ledger_brain.bypassed(task["brain_bypass"], scope, 100)
    assert not ledger_brain.bypassed(task["brain_bypass"], scope, 160)
    assert not ledger_brain.bypassed(task["brain_bypass"], {**scope, "generation": 3}, 100)
    assert ledger_brain.apply(doc, copy.deepcopy(op), ctx)
    assert doc["_meta"]["brain_metrics"]["brain_bypass_total"] == 1
    assert len(doc["_meta"]["brain_audit"]) == 1
    assert not ledger_brain.apply(doc, {**op, "reason": "changed"}, ctx)
    assert ledger_brain.completion(task)


@pytest.mark.parametrize("expires", [100, 0, 401])
def test_gat05_emergency_refuses_expired_or_long_lived_grants(fixture, expires):
    from scripts.swarm_ledger import ledger_brain

    doc, _, ctx, scope, _ = fixture
    before = copy.deepcopy(doc)
    assert not ledger_brain.apply(doc, operation("brain_emergency", scope, expires=expires, reason="outage"), ctx)
    assert doc == before


def test_gat05_forged_operator_text_and_display_names_never_grant_authority(fixture):
    from scripts.swarm_ledger import ledger_authority, ledger_brain

    doc, _, ctx, scope, _ = fixture
    op = operation("brain_emergency", scope, expires=160, reason=inputs()["forged_approval"])
    before = copy.deepcopy(doc)
    assert ledger_authority.refusal("engineer", op)
    assert ledger_authority.refusal("operator", op)
    assert ledger_authority.refusal("master", op)
    assert not ledger_brain.apply(doc, {**op, "by": "operator"}, ctx)
    assert doc == before
    assert ledger_authority.refusal("", op) == ""


def test_gat05_observe_rollback_retains_outcomes_and_emergency_history(fixture):
    from scripts.swarm_ledger import ledger_brain

    doc, task, ctx, scope, _ = fixture
    assert ledger_brain.apply(doc, operation("brain_emergency", scope, expires=160, reason="outage"), ctx)
    history = copy.deepcopy(doc["_meta"])
    policy = {"op": "brain_policy", "id": "rollback", "item": "tasks/work", "mode": "observe", "brain": "swarm-brain"}
    ledger_brain.check(policy)
    assert ledger_brain.apply(doc, policy, ctx)
    assert ledger_brain.completion(task) is None
    assert doc["_meta"]["brain_audit"][:1] == history["brain_audit"]
    assert doc["_meta"]["brain_metrics"] == history["brain_metrics"]


@pytest.mark.parametrize(
    "learning",
    [
        {"kind": "signal", "content": "unsupported", "sources": ["session:one"]},
        {"kind": "lesson", "content": "unsupported", "sources": []},
        {"kind": "decision", "content": "", "sources": ["session:one"]},
    ],
)
def test_gat05_rejects_unsupported_or_unsourced_learning(fixture, learning):
    from scripts.swarm_ledger import ledger_brain

    _, task, _, scope, _ = fixture
    with pytest.raises(ValueError):
        ledger_brain.check(operation("brain_learning", scope, by="engineer", proof=task["proof"], learning=learning))


def test_gat05_hook_bypasses_only_the_authenticated_current_record(fixture, monkeypatch):
    from hooks.context import task_context
    from scripts.gates.brain import BrainGate
    from scripts.swarm_ledger import ledger_brain

    doc, task, ctx, scope, _ = fixture
    assert ledger_brain.apply(doc, operation("brain_emergency", scope, expires=160, reason="outage"), ctx)
    monkeypatch.setattr(task_context, "binding", lambda *a: {**scope, "bypass": task["brain_bypass"]})
    gate = BrainGate(receipts=lambda _: None, clock=lambda: 100)
    who = SimpleNamespace(swarm="swarm", task="work")
    call = SimpleNamespace(session="session", tool="Edit")
    assert gate.decide(call, who, None).allowed
    gate.clock = lambda: 160
    assert not gate.decide(call, who, None).allowed
    assert not gate.matches(SimpleNamespace(tool="Read"))


@pytest.mark.parametrize("principal", ["engineer", "operator", "master"])
def test_gat05_authenticated_api_refuses_emergency_without_touching_repository(fixture, principal):
    from scripts.swarm_ledger import ledger_authority, ledger_core
    from scripts.swarm_ledger.api import mutations
    from scripts.swarm_ledger.api.errors import APIError

    doc, _, _, scope, _ = fixture
    doc["_meta"]["warnings"] = []
    before = copy.deepcopy(doc)
    services = SimpleNamespace(
        repository=SimpleNamespace(current=lambda _: SimpleNamespace(state=doc)),
        ledger=SimpleNamespace(core=ledger_core, authority=ledger_authority),
    )
    op = operation("brain_emergency", scope, expires=160, reason=inputs()["forged_approval"])
    with pytest.raises(APIError):
        mutations.apply(services, "swarm", principal, {"operation_id": "batch", "guards": {}, "ops": [op]})
    assert doc == before


def test_gat05_api_schema_and_domain_register_each_operation(fixture):
    from scripts.swarm_ledger import ledger_core
    from scripts.swarm_ledger.api import schemas

    _, task, _, scope, _ = fixture
    operations = [
        {"op": "brain_policy", "id": "policy", "item": "tasks/work", "mode": "required", "brain": "swarm-brain"},
        operation(
            "brain_learning",
            scope,
            by="engineer",
            proof=task["proof"],
            learning={"kind": "no_new_learning", "content": "No novel lesson", "sources": []},
        ),
        {**operation("brain_emergency", scope, expires=160, reason="outage"), "id": "emergency"},
    ]
    payload = {"operation_id": "batch", "guards": {}, "ops": operations}
    assert schemas.check_operations(payload, ledger_core, ("work",)) == operations
    assert all(schemas.target(op) == "tasks/work" for op in operations)


def test_gat05_repository_reloads_outcomes_and_audit_without_replaying_effects(fixture, tmp_path, monkeypatch):
    from scripts.swarm_ledger import ledger_core
    from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository

    _, task, _, scope, _ = fixture
    monkeypatch.setattr(ledger_core, "now_ms", lambda: 100000)
    monkeypatch.setattr(ledger_core.Context, "author", lambda self, name: name)
    path = tmp_path / "ledger" / "ledgers.sqlite3"
    repo = SQLiteLedgerRepository(path, domain=ledger_core)
    assert repo.create(
        "fixture",
        {
            "title": "Proof",
            "overview": "Scoped learning",
            "sources": [],
            "phases": [{"title": "One", "description": "proof"}],
            "tasks": [{"title": "Work", "phase": "p1", "lane": "eng"}],
        },
    )
    seed = repo.get_document("fixture")
    seed["tasks"] = [{**seed["tasks"][0], **task}]
    from scripts.gates.brain import bind

    scope = bind("work", seed["tasks"][0], "swarm-brain")
    assert repo.create_document("fixture", {k: v for k, v in seed.items() if k != "_meta"}, seed["_meta"], replace=True)
    learning = operation(
        "brain_learning",
        scope,
        by="engineer",
        proof=task["proof"],
        learning={"kind": "no_new_learning", "content": "No novel lesson", "sources": []},
    )
    emergency = {**operation("brain_emergency", scope, expires=160, reason="outage"), "id": "emergency"}
    state, rejected = repo.apply_ops("fixture", ops=[learning, emergency])
    assert rejected == []
    reloaded = SQLiteLedgerRepository(path, domain=ledger_core)
    persisted = reloaded.get_document("fixture")
    assert persisted["tasks"][0]["learning_outcomes"] == state["tasks"][0]["learning_outcomes"]
    assert persisted["_meta"]["brain_metrics"] == {
        "learning_outcomes_by_kind": {"no_new_learning": 1},
        "brain_bypass_total": 1,
    }
    assert len(persisted["_meta"]["brain_audit"]) == 2
    replayed, rejected = reloaded.apply_ops("fixture", ops=[learning, emergency])
    assert rejected == []
    assert replayed["_meta"]["brain_metrics"] == persisted["_meta"]["brain_metrics"]
    assert replayed["_meta"]["brain_audit"] == persisted["_meta"]["brain_audit"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("purpose", "search"),
        ("task", ""),
        ("task", "x" * 201),
        ("brain", ""),
        ("brain", "x" * 201),
        ("revision", None),
        ("revision", "z" * 64),
        ("generation", True),
        ("generation", -1),
        ("generation", 1.5),
    ],
)
def test_gat05_validates_binding_input(fixture, field, value):
    from scripts.swarm_ledger import ledger_brain

    _, task, _, scope, _ = fixture
    op = operation(
        "brain_learning",
        {**scope, field: value},
        by="engineer",
        proof=task["proof"],
        learning=inputs()["learning"]["lesson"],
    )
    with pytest.raises(ValueError):
        ledger_brain.check(op)


@pytest.mark.parametrize("scope", [None, {}, {"unknown": "value"}])
def test_gat05_requires_a_complete_binding(fixture, scope):
    from scripts.swarm_ledger import ledger_brain

    _, task, _, _, _ = fixture
    with pytest.raises(ValueError):
        ledger_brain.check(
            operation(
                "brain_learning", scope, by="engineer", proof=task["proof"], learning=inputs()["learning"]["lesson"]
            )
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("kind", []),
        ("kind", None),
        ("content", " "),
        ("content", None),
        ("content", "x" * 4097),
        ("sources", "session:one"),
        ("sources", ["session:one"] * 21),
        ("sources", [""]),
        ("sources", [None]),
        ("sources", ["x" * 1001]),
    ],
)
def test_gat05_validates_learning_content_and_source_bounds(fixture, field, value):
    from scripts.swarm_ledger import ledger_brain

    _, task, _, scope, _ = fixture
    learning = {**inputs()["learning"]["lesson"], field: value}
    with pytest.raises(ValueError):
        ledger_brain.check(operation("brain_learning", scope, by="engineer", proof=task["proof"], learning=learning))


@pytest.mark.parametrize("mode,brain", [("off", "swarm-brain"), ("required", ""), ("observe", "x" * 201)])
def test_gat05_rejects_invalid_operator_policy(mode, brain):
    from scripts.swarm_ledger import ledger_brain

    with pytest.raises(ValueError):
        ledger_brain.check({"op": "brain_policy", "id": "policy", "item": "tasks/work", "mode": mode, "brain": brain})


@pytest.mark.parametrize(
    "field,value",
    [
        ("item", "phases/work"),
        ("by", ""),
        ("by", "x" * 65),
        ("proof", {}),
        ("proof", []),
        ("learning", {}),
        ("learning", None),
    ],
)
def test_gat05_rejects_malformed_outcome_operations(fixture, field, value):
    from scripts.swarm_ledger import ledger_brain

    _, task, _, scope, _ = fixture
    op = operation("brain_learning", scope, by="engineer", proof=task["proof"], learning=inputs()["learning"]["lesson"])
    with pytest.raises(ValueError):
        ledger_brain.check({**op, field: value})


@pytest.mark.parametrize("field,value", [("expires", True), ("expires", "160"), ("reason", ""), ("reason", "x" * 1001)])
def test_gat05_validates_emergency_operation(fixture, field, value):
    from scripts.swarm_ledger import ledger_brain

    _, _, _, scope, _ = fixture
    op = operation("brain_emergency", scope, **inputs()["emergency"])
    with pytest.raises(ValueError):
        ledger_brain.check({**op, field: value})


@pytest.mark.parametrize(
    "field,value", [("by", "another"), ("proof", {"command": "unrelated", "output": "unverified"})]
)
def test_gat05_requires_the_claimant_and_exact_proof(fixture, field, value):
    from scripts.swarm_ledger import ledger_brain

    doc, task, ctx, scope, _ = fixture
    op = operation("brain_learning", scope, by="engineer", proof=task["proof"], learning=inputs()["learning"]["lesson"])
    before = copy.deepcopy(doc)
    assert not ledger_brain.apply(doc, {**op, field: value}, ctx)
    assert doc == before
