import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from hooks.context import project_cache, task_context
from hooks.memory.api.ingest import Grant
from hooks.observability import consultation
from scripts.gates import brain, entry
from scripts.gates.base import Call, Who
from scripts.swarm_v2.keyspace import Namespace

FIXTURE = Path(__file__).parents[1] / "fixtures/swarm_v2/consultation-effectiveness.json"
INPUTS = json.loads(FIXTURE.read_text())
NOW = 1000.0


@pytest.fixture
def world(monkeypatch, tmp_path):
    namespace = Namespace("fixture-installation", INPUTS["brain"], INPUTS["project"])
    monkeypatch.setattr("hooks.config.AGENTIHOOKS_HOME", tmp_path)
    monkeypatch.setattr(project_cache, "namespace", lambda: namespace)
    monkeypatch.setattr(consultation, "runtime_namespace", lambda: namespace)
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "fixture-swarm")
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", "fixture-task")
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")
    monkeypatch.delenv("AGENTIHOOKS_TASK_CONTEXT_OFF", raising=False)
    monkeypatch.setattr(task_context.time, "time", lambda: NOW)
    bound = brain.bind("fixture-task", {"title": "Fixture", "claim_generation": 1}, namespace.brain)
    monkeypatch.setattr(task_context, "binding", lambda *args: bound)
    path = tmp_path / "brain/consultation.sqlite3"
    metrics = consultation.Metrics(path, namespace, operators=frozenset({INPUTS["operator"]}))
    grant = Grant("fixture-session", namespace.brain, frozenset({namespace.project}))
    operator = replace(grant, principal=INPUTS["operator"])
    return metrics, grant, operator, bound, path, namespace


def delivery(name="useful", **changes):
    return {
        "kind": "delivery",
        "session": "fixture-session",
        "task": "fixture-task",
        "context": name,
        "purpose": "task_transition",
        "target": "claude",
        "pack_bytes": 20,
        "delivery": "delivered",
        "retrieval": "delivered",
        "receipt_age_seconds": 0.0,
        **changes,
    }


def test_a_fixture_separates_delivery_reference_and_reviewed_usefulness(world):
    metrics, grant, operator, _, _, _ = world
    for row in INPUTS["cases"][:2]:
        identifier = metrics.record(grant, row["name"], delivery(row["name"]))
        if row["reference"]:
            metrics.record(
                grant,
                "reference",
                {
                    "kind": "reference",
                    "session": grant.principal,
                    "task": "fixture-task",
                    "context": row["name"],
                    "lesson": "lesson-private",
                    "incident": "incident-private",
                },
            )
        metrics.review(operator, identifier, {"usefulness": row["usefulness"]})
    report = metrics.report(grant)
    assert report["context_delivered"] == 2
    assert report["context_referenced"] == 1
    assert report["operator_reviewed_useful"] == 1
    assert report["operator_reviewed_irrelevant"] == 1
    assert report["brain_gate_false_block_rate"] is None
    assert report["correlations"][0]["references"] == 1


def test_b_http_success_without_target_delivery_is_never_context(world):
    metrics, grant, _, _, _, _ = world
    case = INPUTS["cases"][2]
    assert task_context.offer(grant.principal, "PreToolUse", case["pack"], injectable=False) is None
    report = metrics.report(grant)
    assert report["context_delivered"] == 0
    assert report["context_delivery_failures"] == 1
    assert report["delivery_outcomes"] == {"unsupported_event": 1}
    with pytest.raises(ValueError, match="delivered"):
        metrics.record(
            grant,
            "invented-reference",
            {
                "kind": "reference",
                "session": grant.principal,
                "task": "fixture-task",
                "context": "missing",
                "lesson": "lesson",
                "incident": "incident",
            },
        )


def test_a_hook_delivery_records_only_metadata_and_deduplicates(world):
    metrics, grant, _, _, _, _ = world
    pack = INPUTS["cases"][0]["pack"]
    assert task_context.offer(grant.principal, "PreToolUse", pack, injectable=True) == pack
    assert task_context.offer(grant.principal, "PreToolUse", pack, injectable=True) is None
    report = metrics.report(grant)
    assert report["context_delivered"] == 1
    assert report["context_referenced"] == 0
    assert report["operator_reviewed_useful"] == 0
    assert report["delivery_outcomes"] == {"delivered": 1, "deduplicated": 1}
    rows = metrics.events(grant)
    delivered = next(row for row in rows if row.get("delivery") == "delivered")
    assert delivered["purpose"] == "task_transition"
    assert delivered["pack_bytes"] == len(pack.encode())
    assert delivered["receipt_age_seconds"] == 0
    assert pack not in json.dumps(rows)
    assert grant.principal not in json.dumps(rows)


def test_b_scope_and_operator_authority_come_from_grants(world):
    metrics, grant, operator, _, path, namespace = world
    identifier = metrics.record(grant, "useful", delivery())
    before = path.read_bytes()
    with pytest.raises(PermissionError):
        metrics.review(grant, identifier, {"usefulness": "useful"})
    with pytest.raises(PermissionError):
        metrics.report(replace(operator, corpus="foreign"))
    with pytest.raises(PermissionError):
        metrics.events(replace(grant, projects=frozenset()))
    with pytest.raises(ValueError):
        metrics.record(grant, "spoof", {**delivery(), "operator": True, "prompt": "private"})
    assert path.read_bytes() == before
    other = consultation.Metrics(path, replace(namespace, project="foreign"))
    with pytest.raises(PermissionError):
        other.report(grant)
    foreign = replace(grant, projects=frozenset({"foreign"}))
    assert other.report(foreign)["context_delivered"] == 0


def test_a_reviewed_gate_false_blocks_and_missed_opportunities(world):
    metrics, grant, operator, _, _, _ = world
    denied = metrics.record(
        grant,
        "denied",
        {
            "kind": "gate",
            "session": grant.principal,
            "task": "fixture-task",
            "context": "missing",
            "purpose": "task_transition",
            "gate_result": "deny",
            "receipt_age_seconds": None,
        },
    )
    allowed = metrics.record(
        grant,
        "allowed",
        {
            "kind": "gate",
            "session": grant.principal,
            "task": "fixture-task",
            "context": "missing",
            "purpose": "task_transition",
            "gate_result": "allow",
            "receipt_age_seconds": None,
        },
    )
    metrics.review(operator, denied, {"false_block": True})
    metrics.review(operator, allowed, {"missed_opportunity": True})
    assert metrics.report(grant)["brain_gate_false_block_rate"] == 1.0
    assert metrics.report(grant)["operator_reviewed_missed_opportunities"] == 1
    assert metrics.report(grant)["operator_overrides"] == 0


@pytest.mark.parametrize("mode,result,code", [("enforce", "deny", 2), ("observe", "observe", 0)])
def test_c_gate_modes_keep_outcomes_without_changing_behavior(world, monkeypatch, mode, result, code):
    metrics, grant, _, _, _, _ = world
    who = Who(name="fixture-worker", swarm="fixture-swarm", task="fixture-task")
    monkeypatch.setattr(entry.Who, "from_env", lambda env: who)
    monkeypatch.setattr(entry.modes, "swarm_gates", lambda *args: {})
    monkeypatch.setattr(entry.modes, "mode", lambda *args: mode)
    monkeypatch.setattr(entry.lift, "lifted", lambda *args: False)
    monkeypatch.setattr(entry.lift, "held", lambda *args, **kwargs: False)
    monkeypatch.setattr(entry, "_record", lambda *args: None)
    call = Call("Write", {"content": "sensitive full prompt"}, session=grant.principal)
    assert entry.evaluate(brain.BrainGate(receipts=lambda _: {}, clock=lambda: NOW), call, environ={}) == code
    assert metrics.report(grant)["gate_results"] == {result: 1}
    assert "sensitive full prompt" not in json.dumps(metrics.events(grant))


def test_a_operator_lift_is_recorded_separately_from_a_false_block(world, monkeypatch):
    metrics, grant, _, _, _, _ = world
    monkeypatch.setattr(entry.Who, "from_env", lambda env: Who(swarm="fixture-swarm", task="fixture-task"))
    monkeypatch.setattr(entry.modes, "swarm_gates", lambda *args: {})
    monkeypatch.setattr(entry.modes, "mode", lambda *args: "enforce")
    monkeypatch.setattr(entry.lift, "lifted", lambda *args: True)
    monkeypatch.setattr(entry, "_record", lambda *args: None)
    call = Call("Write", session=grant.principal)
    assert entry.evaluate(brain.BrainGate(receipts=lambda _: {}), call, environ={}) == 0
    report = metrics.report(grant)
    assert report["operator_overrides"] == 1
    assert report["brain_gate_false_block_rate"] is None


def test_c_outage_recovery_replay_and_observe_rollback_retain_history(world):
    metrics, grant, operator, _, path, namespace = world
    failed = f"{project_cache.UNREACHABLE} unavailable"
    assert task_context.offer(grant.principal, "PreToolUse", failed, injectable=True) == failed
    degraded = metrics.report(grant)
    assert degraded["degraded"] is True
    assert degraded["context_delivered"] == 0
    assert degraded["context_service_failures"] == 1
    assert task_context.offer(grant.principal, "PreToolUse", "Recovered useful context", injectable=True)
    reopened = consultation.Metrics(path, namespace, operators=frozenset({operator.principal}))
    assert reopened.report(grant)["degraded"] is False
    assert reopened.report(grant)["context_service_failures"] == 1
    first = reopened.record(grant, "stable-operation", delivery())
    assert reopened.record(grant, "stable-operation", delivery()) == first
    with pytest.raises(ValueError, match="replay"):
        reopened.record(grant, "stable-operation", delivery(pack_bytes=99))
    assert reopened.report(grant)["context_delivered"] == 2


def test_b_routine_metrics_reject_private_content_and_keep_opaque_correlations(world):
    metrics, grant, _, _, _, _ = world
    metrics.record(grant, "useful", delivery())
    metrics.record(
        grant,
        "ref",
        {
            "kind": "reference",
            "session": grant.principal,
            "task": "fixture-task",
            "context": "useful",
            "lesson": "private lesson name",
            "incident": "private incident name",
        },
    )
    serialized = json.dumps(metrics.events(grant)) + json.dumps(metrics.report(grant))
    for private in ("private lesson name", "private incident name", "fixture-session", "fixture-task"):
        assert private not in serialized
    with pytest.raises(ValueError):
        metrics.record(grant, "private", {**delivery(), "prompt": "entire transcript"})


def test_b_telemetry_failure_does_not_change_context_delivery(world, monkeypatch, caplog):
    _, grant, _, _, _, _ = world

    def unavailable(*args, **kwargs):
        raise OSError("private error contents")

    monkeypatch.setattr(consultation.Metrics, "record", unavailable)
    assert task_context.offer(grant.principal, "PreToolUse", "context", injectable=True) == "context"
    assert "consultation metrics unavailable" in caplog.text
    assert "private error contents" not in caplog.text


@pytest.mark.parametrize("damaged", [{"purpose": "task"}, {"at": "private corrupt value"}])
def test_b_damaged_receipt_metrics_never_change_delivery_or_gate_decisions(world, monkeypatch, caplog, damaged):
    _, grant, _, _, _, _ = world
    monkeypatch.setattr(task_context, "consultation", lambda _: damaged)
    assert task_context.offer(grant.principal, "PreToolUse", "context", injectable=True) == "context"
    who = Who(swarm="fixture-swarm", task="fixture-task")
    monkeypatch.setattr(entry.Who, "from_env", lambda env: who)
    monkeypatch.setattr(entry.modes, "swarm_gates", lambda *args: {})
    monkeypatch.setattr(entry.modes, "mode", lambda *args: "enforce")
    monkeypatch.setattr(entry.lift, "lifted", lambda *args: False)
    monkeypatch.setattr(entry.lift, "held", lambda *args, **kwargs: False)
    monkeypatch.setattr(entry, "_record", lambda *args: None)
    assert (
        entry.evaluate(brain.BrainGate(receipts=lambda _: {}), Call("Write", session=grant.principal), environ={}) == 2
    )
    assert caplog.text.count("consultation metrics unavailable") == 2
    assert "private corrupt value" not in caplog.text


def test_b_gate_exceptions_keep_fail_open_outcome_without_private_details(world, monkeypatch):
    metrics, grant, _, _, _, _ = world
    who = Who(swarm="fixture-swarm", task="fixture-task")
    monkeypatch.setattr(entry.Who, "from_env", lambda env: who)
    monkeypatch.setattr(entry.modes, "swarm_gates", lambda *args: {})
    monkeypatch.setattr(entry.modes, "mode", lambda *args: "enforce")
    logs = []
    monkeypatch.setattr(entry, "_record", lambda who, row, home: logs.append(asdict(row)))
    gate = brain.BrainGate()

    def unavailable(*args):
        raise RuntimeError("private exception contents")

    monkeypatch.setattr(gate, "decide", unavailable)
    assert entry.evaluate(gate, Call("Write", session=grant.principal), environ={}) == 0
    assert metrics.report(grant)["gate_results"] == {"fail-open": 1}
    assert "private exception contents" not in json.dumps(logs)


def test_c_absent_refresh_after_a_current_delivery_is_not_a_failure(world):
    metrics, grant, _, _, _, _ = world
    assert task_context.offer(grant.principal, "PreToolUse", "context", injectable=True)
    assert task_context.offer(grant.principal, "PreToolUse", None, injectable=True) is None
    assert metrics.report(grant)["context_delivery_failures"] == 0
    assert metrics.report(grant)["degraded"] is False
