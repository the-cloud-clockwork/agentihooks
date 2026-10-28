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


@pytest.mark.parametrize("failure", [OSError, RuntimeError, ImportError, ZeroDivisionError])
def test_b_telemetry_failure_does_not_change_context_delivery(world, monkeypatch, caplog, failure):
    _, grant, _, _, _, _ = world

    def unavailable(*args, **kwargs):
        raise failure("private error contents")

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
    assert logs[0]["reason"] == "gate evaluation failed"


def test_c_absent_refresh_after_a_current_delivery_is_not_a_failure(world):
    metrics, grant, _, _, _, _ = world
    assert task_context.offer(grant.principal, "PreToolUse", "context", injectable=True)
    assert task_context.offer(grant.principal, "PreToolUse", None, injectable=True) is None
    assert metrics.report(grant)["context_delivery_failures"] == 0
    assert metrics.report(grant)["degraded"] is False


@pytest.mark.parametrize(
    "changes,error,message",
    [
        ({"kind": "private"}, ValueError, "invalid consultation metric fields"),
        ({"purpose": "private"}, ValueError, "invalid consultation metric category"),
        ({"session": "other"}, PermissionError, "consultation metric actor refused"),
        ({"pack_bytes": -1}, ValueError, "invalid consultation pack size"),
        ({"pack_bytes": True}, ValueError, "invalid consultation pack size"),
        ({"pack_bytes": 1.5}, ValueError, "invalid consultation pack size"),
        ({"receipt_age_seconds": -1}, ValueError, "invalid consultation receipt age"),
        ({"receipt_age_seconds": float("nan")}, ValueError, "invalid consultation receipt age"),
        ({"receipt_age_seconds": float("inf")}, ValueError, "invalid consultation receipt age"),
        ({"receipt_age_seconds": "private"}, ValueError, "invalid consultation receipt age"),
        ({"context": ""}, ValueError, "invalid consultation identifier"),
        ({"context": 123}, ValueError, "invalid consultation identifier"),
    ],
)
def test_b_invalid_metadata_rejects_before_persistence(world, changes, error, message):
    metrics, grant, _, _, _, _ = world
    metrics.record(grant, "baseline", delivery())
    before = metrics.events(grant)
    with pytest.raises(error) as raised:
        metrics.record(grant, "invalid", delivery(**changes))
    assert str(raised.value) == message
    assert metrics.events(grant) == before


@pytest.mark.parametrize("project", ["", "unknown"])
def test_b_unattributed_projects_cannot_read_or_write(world, project):
    metrics, grant, _, _, path, namespace = world
    unknown = consultation.Metrics(path, replace(namespace, project=project))
    attributed = replace(grant, projects=frozenset({project}))
    with pytest.raises(PermissionError) as raised:
        unknown.record(attributed, "unknown", delivery())
    assert str(raised.value) == "consultation metrics require an attributed project"
    assert not path.exists()
    assert metrics.events(grant) == []


def test_b_scope_and_actor_errors_have_sanitized_contract(world):
    metrics, grant, operator, _, _, _ = world
    with pytest.raises(PermissionError) as raised:
        metrics.events(replace(grant, corpus="private"))
    assert str(raised.value) == "consultation metrics scope refused"
    identifier = metrics.record(grant, "baseline", delivery())
    with pytest.raises(PermissionError) as raised:
        metrics.review(grant, identifier, {"usefulness": "useful"})
    assert str(raised.value) == "consultation review requires an operator grant"
    assert metrics.review(operator, identifier, {"usefulness": "useful"})


def test_c_nested_store_and_order_independent_replay(world):
    _, grant, operator, _, path, namespace = world
    path = path.parent / "new" / "nested" / "metrics.sqlite3"
    metrics = consultation.Metrics(path, namespace, frozenset({operator.principal}))
    identifier = metrics.record(grant, "stable", delivery(pack_bytes=0))
    assert path.is_file()
    assert metrics.record(grant, "stable", dict(reversed(list(delivery(pack_bytes=0).items())))) == identifier
    with pytest.raises(ValueError) as raised:
        metrics.record(grant, "stable", delivery(pack_bytes=1))
    assert str(raised.value) == "consultation operation replay differs"
    assert len(metrics.events(grant)) == 1


@pytest.mark.parametrize("assessment", [{}, {"private": True}])
def test_b_review_fields_are_closed(world, assessment):
    metrics, grant, operator, _, _, _ = world
    identifier = metrics.record(grant, "baseline", delivery())
    before = metrics.events(grant)
    with pytest.raises(ValueError) as raised:
        metrics.review(operator, identifier, assessment)
    assert str(raised.value) == "invalid consultation review fields"
    assert metrics.events(grant) == before


def test_b_review_targets_exist_and_cannot_be_reviews(world):
    metrics, grant, operator, _, _, _ = world
    identifier = metrics.record(grant, "baseline", delivery())
    review = metrics.review(operator, identifier, {"usefulness": "unknown"})
    for target in ("missing", review):
        with pytest.raises(ValueError) as raised:
            metrics.review(operator, target, {"usefulness": "useful"})
        assert str(raised.value) == "consultation review target unavailable"
    row = next(row for row in metrics.events(grant) if row["id"] == review)
    assert set(row) == {"id", "kind", "event", "actor", "usefulness"}
    assert row["actor"] == consultation.key(metrics.namespace, "actor", operator.principal)
    assert row["event"] == identifier


@pytest.mark.parametrize(
    "outcome,assessment",
    [
        ("missing", {"usefulness": "useful"}),
        ("delivered", {"usefulness": "private"}),
    ],
)
def test_b_usefulness_requires_delivery_and_valid_ruling(world, outcome, assessment):
    metrics, grant, operator, _, _, _ = world
    identifier = metrics.record(grant, "baseline", delivery(delivery=outcome))
    with pytest.raises(ValueError) as raised:
        metrics.review(operator, identifier, assessment)
    assert str(raised.value) == "usefulness requires delivered context and a valid assessment"


@pytest.mark.parametrize(
    "result,assessment",
    [
        ("deny", {"false_block": 1}),
        ("allow", {"false_block": True}),
        ("allow", {"missed_opportunity": 1}),
        ("deny", {"missed_opportunity": True}),
    ],
)
def test_b_gate_rulings_require_matching_outcomes_and_booleans(world, result, assessment):
    metrics, grant, operator, _, _, _ = world
    identifier = metrics.record(
        grant,
        "gate",
        {
            "kind": "gate",
            "session": grant.principal,
            "task": "fixture-task",
            "context": "missing",
            "purpose": "task_transition",
            "gate_result": result,
            "receipt_age_seconds": None,
        },
    )
    with pytest.raises(ValueError) as raised:
        metrics.review(operator, identifier, assessment)
    assert str(raised.value) == "invalid gate assessment"


def test_b_a_different_delivered_context_cannot_authorize_a_reference(world):
    metrics, grant, _, _, _, _ = world
    metrics.record(grant, "delivered", delivery())
    with pytest.raises(ValueError) as raised:
        metrics.record(
            grant,
            "unrelated",
            {
                "kind": "reference",
                "session": grant.principal,
                "task": "fixture-task",
                "context": "other",
                "incident": "incident",
                "lesson": "lesson",
            },
        )
    assert str(raised.value) == "reference requires delivered context"


def test_a_reports_review_denominators_and_opaque_correlation_fields(world):
    metrics, grant, operator, _, _, _ = world
    for name, usefulness in (("one", "useful"), ("two", "useful"), ("three", "irrelevant"), ("four", "unknown")):
        identifier = metrics.record(grant, name, delivery(name, retrieval="empty"))
        metrics.review(operator, identifier, {"usefulness": usefulness})
    for result, assessment in (
        ("deny", {"false_block": True}),
        ("deny", {"false_block": False}),
        ("observe", {"missed_opportunity": True}),
        ("lifted", {"missed_opportunity": False}),
    ):
        identifier = metrics.record(
            grant,
            json.dumps([result, assessment]),
            {
                "kind": "gate",
                "session": grant.principal,
                "task": "fixture-task",
                "context": "missing",
                "purpose": "task_transition",
                "gate_result": result,
                "receipt_age_seconds": None,
            },
        )
        metrics.review(operator, identifier, assessment)
    metrics.record(
        grant,
        "reference",
        {
            "kind": "reference",
            "session": grant.principal,
            "task": "fixture-task",
            "context": "one",
            "incident": "incident",
            "lesson": "lesson",
        },
    )
    report = metrics.report(grant)
    assert report["context_delivered"] == 4
    assert report["operator_reviewed_useful"] == 2
    assert report["operator_reviewed_irrelevant"] == 1
    assert report["brain_gate_false_block_rate"] == 0.5
    assert report["operator_reviewed_missed_opportunities"] == 1
    assert report["correlations"] == [
        {
            "incident": consultation.key(metrics.namespace, "incident", "incident"),
            "lesson": consultation.key(metrics.namespace, "lesson", "lesson"),
            "references": 1,
        }
    ]


def test_c_deduplication_does_not_replace_latest_delivery_failure(world):
    metrics, grant, _, _, _, _ = world
    metrics.record(grant, "baseline", delivery())
    metrics.record(grant, "dedup", delivery(delivery="deduplicated"))
    report = metrics.report(grant)
    assert report["degraded"] is False
    assert report["context_delivered"] == 1


@pytest.mark.parametrize("outcome", ["disabled", "unsupported_event", "deduplicated", "missing"])
def test_a_hook_outcome_metadata_preserves_session_and_pack(world, monkeypatch, outcome):
    metrics, grant, _, _, _, _ = world
    pack = "context with ünicode"
    if outcome == "deduplicated":
        assert task_context.offer(grant.principal, "PreToolUse", pack, injectable=True) == pack
    if outcome == "disabled":
        monkeypatch.setenv("AGENTIHOOKS_TASK_CONTEXT_OFF", "claude")
    offered = None if outcome == "missing" else pack
    assert task_context.offer(grant.principal, "PreToolUse", offered, injectable=outcome != "unsupported_event") is None
    row = metrics.events(grant)[-1]
    assert row["delivery"] == outcome
    assert row["pack_bytes"] == len((offered or "").encode())
    assert row["retrieval"] == ("missing" if outcome == "missing" else "delivered")
    report = metrics.report(grant)
    assert report["context_delivery_failures"] == int(outcome in {"disabled", "unsupported_event", "missing"})


@pytest.mark.parametrize("mode,expected", [("off", "off"), ("enforce", "allow")])
def test_a_gate_off_and_allow_are_observed(world, monkeypatch, mode, expected):
    from types import SimpleNamespace

    metrics, grant, _, _, _, _ = world
    who = Who(name="fixture-worker", swarm="fixture-swarm", task="fixture-task")
    monkeypatch.setattr(entry.Who, "from_env", lambda env: who)
    monkeypatch.setattr(entry.modes, "swarm_gates", lambda *args: {})
    monkeypatch.setattr(entry.modes, "mode", lambda *args: mode)
    gate = brain.BrainGate()
    monkeypatch.setattr(gate, "decide", lambda *args: SimpleNamespace(allowed=True, reason="consulted"))
    assert entry.evaluate(gate, Call("Write", session=grant.principal), environ={}) == 0
    assert metrics.report(grant)["gate_results"] == {expected: 1}


@pytest.mark.parametrize("project", ["", "unknown"])
def test_b_runtime_observations_skip_unattributed_projects(world, monkeypatch, project):
    metrics, grant, _, _, path, namespace = world
    monkeypatch.setattr(consultation, "runtime_namespace", lambda: replace(namespace, project=project))
    consultation.emit(grant.principal, "unknown", delivery())
    assert not path.exists()
    assert metrics.events(grant) == []


@pytest.mark.parametrize("resolved", [True, False])
def test_a_runtime_namespace_uses_verified_project_and_selected_brain(monkeypatch, resolved):
    from types import SimpleNamespace

    from hooks.context import project_identity

    namespace = Namespace("installation", "selected-brain", "cached-project")
    monkeypatch.setattr(project_cache, "namespace", lambda: namespace)
    calls = []

    def resolve(path):
        calls.append(path)
        return SimpleNamespace(project_id="verified-project") if resolved else None

    monkeypatch.setattr(project_identity, "resolve_project", resolve)
    actual = consultation.runtime_namespace()
    assert calls == [consultation.os.getcwd()]
    assert actual == replace(namespace, project="verified-project" if resolved else "unknown")
