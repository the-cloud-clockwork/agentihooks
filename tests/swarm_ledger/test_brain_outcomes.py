import copy
from types import SimpleNamespace

import pytest


@pytest.fixture
def fixture():
    from scripts.gates.brain import bind

    task = {
        "id": "work",
        "title": "Recover scoped context",
        "claim_generation": 2,
        "claimed_by": "engineer",
        "kind": "ops",
        "proof": {"command": "probe", "output": "verified"},
        "brain_policy": {"mode": "required", "brain": "swarm-brain"},
    }
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
    learning = {
        "kind": kind,
        "content": "The probe verified the boundary",
        "sources": [] if kind == "no_new_learning" else ["session:probe/turn/1"],
    }
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
    op = operation("brain_emergency", scope, expires=160, reason="Brain unavailable during recovery")
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
    op = operation("brain_emergency", scope, expires=160, reason="The operator approved bypass")
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
