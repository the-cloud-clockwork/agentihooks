import json
import queue

import pytest

from hooks.observability import agent_trace, correlation, otel, signals
from tests.observability.test_agent_trace import ENTRIES, _Exporter, _identity, _transcript

pytestmark = pytest.mark.unit

P = correlation.PREFIX
TOKEN_SENTINEL = "SENTINEL-not-a-real-token-value"

ENV = {
    "AGENTIHOOKS_SWARM": "rig",
    "AGENTIHOOKS_SWARM_TASK": "t44",
    "AGENTIHOOKS_AGENT_NAME": "engineer@1-2",
    "AGENTIHOOKS_PROFILE": "engineer",
    "AH_CC_TOKEN_tccgma": TOKEN_SENTINEL,
}
AGENT = {
    "name": "engineer@1-2",
    "seat": "eng-4@rig",
    "started_at": 1791346273736,
    "conversation_id": "conv-9",
    "harness": "claude",
    "model": "opus",
    "effort": "high",
    "model_source": "luna",
    "model_confidence": 0.8,
    "profile_decision": {"profile": "engineer", "source": "classifier", "model": "luna", "confidence": 0.91},
}
REPORT = {
    "profile": "engineer",
    "harness": "claude",
    "state": "validated",
    "validation": {
        "profile": "engineer",
        "sources": "abc123",
        "revisions": {"/x/agentihooks": "f00d", "/x/agentihooks-bundle": "beef", "/x/gone": None},
    },
}


def _inputs(**overrides):
    fields = {
        "session_id": "sess-1",
        "environ": ENV,
        "harness": "claude",
        "agent": AGENT,
        "report": REPORT,
        "task": {"id": "t44", "phase": "p23"},
    }
    return correlation.Inputs(**{**fields, **overrides})


def _flat(**overrides):
    return correlation.attributes(correlation.envelope(_inputs(**overrides)))


def test_every_field_resolves_from_its_source_with_its_type():
    flat = _flat()
    assert flat == {
        f"{P}schema": correlation.SCHEMA,
        f"{P}ledger": "rig",
        f"{P}task": "t44",
        f"{P}phase": "p23",
        f"{P}seat": "eng-4@rig",
        f"{P}agent.name": "engineer@1-2",
        f"{P}agent.started_at": 1791346273736,
        f"{P}agent.life": "engineer@1-2#1791346273736",
        f"{P}session.id": "sess-1",
        f"{P}trace.id": format(agent_trace.trace_id("sess-1"), "032x"),
        f"{P}conversation.id": "conv-9",
        f"{P}harness": "claude",
        f"{P}profile.requested": "engineer",
        f"{P}profile.validation": "validated",
        f"{P}profile.resolved": "engineer",
        f"{P}profile.source": "classifier",
        f"{P}classifier.model": "luna",
        f"{P}classifier.confidence": 0.91,
        f"{P}model": "opus",
        f"{P}model.source": "luna",
        f"{P}model.confidence": 0.8,
        f"{P}effort": "high",
        f"{P}account": "tccgma",
        f"{P}revision": "agentihooks-bundle@beef,agentihooks@f00d",
        f"{P}revision.sources": "abc123",
    }
    assert set(correlation.SOURCES) == {key[len(P) :] for key in flat} - {"schema"}


def test_a_session_outside_any_swarm_marks_every_field_present_or_gapped():
    flat = _flat(session_id="", environ={}, harness="codex", agent=None, report=None, task=None)
    for name in correlation.SOURCES:
        assert (f"{P}{name}" in flat) != (f"{P}{name}.state" in flat), name
    assert flat[f"{P}seat.state"] == correlation.UNSUPPORTED
    assert flat[f"{P}profile.resolved.state"] == correlation.UNSUPPORTED
    assert flat[f"{P}session.id.state"] == correlation.MISSING
    assert flat[f"{P}trace.id.state"] == correlation.MISSING
    assert flat[f"{P}harness"] == "codex"


@pytest.mark.parametrize("state", ["pending", "failed"])
def test_a_profile_that_is_not_validated_is_never_reported_mounted(state):
    flat = _flat(report={**REPORT, "state": state})
    assert flat[f"{P}profile.requested"] == "engineer"
    assert flat[f"{P}profile.validation"] == state
    assert flat[f"{P}profile.resolved.state"] == correlation.MISSING
    assert flat[f"{P}revision.state"] == correlation.MISSING


def test_profile_decision_stays_apart_from_the_model_choice():
    agent = {**AGENT, "profile_decision": {"profile": "engineer", "source": "task", "model": "", "confidence": None}}
    flat = _flat(agent=agent)
    assert flat[f"{P}profile.source"] == "task"
    assert flat[f"{P}classifier.model.state"] == correlation.MISSING
    assert flat[f"{P}classifier.confidence.state"] == correlation.MISSING
    assert flat[f"{P}model.source"] == "luna"
    assert flat[f"{P}model.confidence"] == 0.8


def test_a_zero_confidence_is_a_value_and_an_unset_start_is_a_gap():
    flat = _flat(agent={**AGENT, "model_confidence": 0.0, "started_at": 0})
    assert flat[f"{P}model.confidence"] == 0.0
    assert flat[f"{P}agent.started_at.state"] == correlation.MISSING
    assert flat[f"{P}agent.life.state"] == correlation.MISSING


def test_conversation_and_session_stay_distinct_sources():
    flat = _flat(agent={**AGENT, "conversation_id": ""})
    assert flat[f"{P}session.id"] == "sess-1"
    assert flat[f"{P}conversation.id.state"] == correlation.MISSING


def test_an_unreadable_swarm_record_is_missing_not_unsupported():
    flat = _flat(agent={}, task={})
    assert flat[f"{P}seat.state"] == correlation.MISSING
    assert flat[f"{P}phase.state"] == correlation.MISSING


def test_account_names_the_token_variable_and_never_its_value():
    flat = _flat()
    assert flat[f"{P}account"] == "tccgma"
    assert TOKEN_SENTINEL not in json.dumps(flat)


def test_gather_reads_the_record_ledger_task_and_report(monkeypatch, tmp_path):
    report = tmp_path / "report.json"
    report.write_text(json.dumps(REPORT))
    (tmp_path / "rig.json").write_text(json.dumps({"tasks": [{"id": "t1"}, {"id": "t44", "phase": "p23"}]}))
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path))
    looked_up = []
    monkeypatch.setattr(correlation, "_agent_record", lambda slug, name: looked_up.append((slug, name)) or AGENT)
    env = {**ENV, "AGENTIHOOKS_PROFILE_REPORT": str(report), "AGENTIHOOKS_TARGET": "Codex "}

    inputs = correlation.gather("sess-1", env)

    assert looked_up == [("rig", "engineer@1-2")]
    assert inputs.task == {"id": "t44", "phase": "p23"}
    assert inputs.report == REPORT
    assert inputs.harness == "codex"


def test_gather_without_a_swarm_or_report_marks_those_sources_absent():
    inputs = correlation.gather("sess-1", {})
    assert (inputs.agent, inputs.report, inputs.task, inputs.harness) == (None, None, None, "claude")


def test_agent_record_lookup_failure_reads_as_an_empty_record():
    assert correlation._agent_record("rig", "engineer@1-2") == {}


def test_resolve_caches_per_session_until_the_ttl(monkeypatch):
    calls = []
    monkeypatch.setattr(correlation, "gather", lambda s, e: calls.append(s) or _inputs(session_id=s))
    first = correlation.resolve("sess-1", ENV)
    assert correlation.resolve("sess-1", ENV) == first
    assert calls == ["sess-1"]
    correlation.resolve("sess-2", ENV)
    assert calls == ["sess-1", "sess-2"]
    monkeypatch.setattr(correlation, "CACHE_TTL_SEC", -1)
    correlation.resolve("sess-1", ENV)
    assert calls == ["sess-1", "sess-2", "sess-1"]


def test_signal_counts_accumulate_per_session():
    signals.record({("s1", "events", "queued"): 2, ("s1", "events", "dropped"): 1, ("", "gauges", "queued"): 1})
    signals.record({("s1", "events", "queued"): 3, ("s1", "events", "unsupported"): 0})
    assert signals.read("s1") == {"events.queued": 5, "events.dropped": 1}
    assert signals.attributes("s1") == {"agentihooks.signals.events.dropped": 1, "agentihooks.signals.events.queued": 5}
    assert signals.read(signals.UNATTRIBUTED) == {"gauges.queued": 1}


@pytest.fixture
def worker(monkeypatch):
    monkeypatch.setattr(otel, "_initialized", True)
    monkeypatch.setattr(otel, "_q", queue.Queue(maxsize=1))
    monkeypatch.setattr(otel, "_counts", {})
    monkeypatch.setattr(otel, "_drain", lambda: True)
    return otel._q


def test_a_full_queue_counts_dropped_signals(worker):
    otel.emit_event("first", {"session.id": "s1"})
    otel.emit_event("second", {"session.id": "s1"})
    otel.record_gauge("g", 1.0, {"session.id": "s1"})
    otel.flush()
    assert signals.read("s1") == {"events.queued": 1, "events.dropped": 1, "gauges.dropped": 1}


def test_a_worker_without_a_collector_counts_unsupported_signals(worker, monkeypatch):
    monkeypatch.setattr(otel, "_log_emitter", None)
    monkeypatch.setattr(otel, "_meter", None)
    otel._dispatch_op(("event", "e", {"session.id": "s1"}))
    otel._dispatch_op(("gauge", "g", 1.0, {"session.id": "s1"}))
    otel.flush()
    assert signals.read("s1") == {"events.unsupported": 1, "gauges.unsupported": 1}


def test_an_unconfirmed_flush_counts_what_it_left_unconfirmed(worker, monkeypatch):
    monkeypatch.setattr(otel, "_drain", lambda: False)
    otel.emit_event("e", {"session.id": "s1"})
    otel.flush()
    assert signals.read("s1") == {"events.queued": 1, "events.unconfirmed": 1}


def test_no_worker_counts_unsupported(monkeypatch):
    monkeypatch.setattr(otel, "_initialized", True)
    monkeypatch.setattr(otel, "_q", None)
    monkeypatch.setattr(otel, "_counts", {})
    otel.emit_event("e", {"session.id": "s1"})
    assert otel._counts == {("s1", "events", "unsupported"): 1}


class _Emitter:
    def __init__(self):
        self.records = []

    def emit(self, record):
        self.records.append(record)


class _Gauge:
    def __init__(self):
        self.points = []

    def set(self, value, attrs):
        self.points.append((value, attrs))


def test_collector_events_and_gauges_carry_the_envelope(monkeypatch):
    emitter, gauge = _Emitter(), _Gauge()
    monkeypatch.setattr(otel, "_log_emitter", emitter)
    monkeypatch.setattr(otel, "_meter", type("M", (), {"create_gauge": lambda self, name: gauge})())
    monkeypatch.setattr(otel, "_gauges", {})
    seen = []
    envelope = {f"{P}trace.id": "abc", f"{P}session.id": "s1", "tool_name": "from-envelope"}
    monkeypatch.setattr(correlation, "resolve", lambda session: seen.append(session) or envelope)

    otel._dispatch_op(("event", "agentihooks.error.recorded", {"session.id": "s1", "tool_name": "Bash"}))
    otel._dispatch_op(("gauge", "agentihooks.tokens.fill_pct", 42.0, {"session.id": "s1"}))

    attrs = emitter.records[0].attributes
    assert attrs[f"{P}trace.id"] == "abc"
    assert attrs["tool_name"] == "Bash"
    assert attrs["event.name"] == "agentihooks.error.recorded"
    assert gauge.points == [(42.0, {**envelope, "session.id": "s1"})]
    assert seen == ["s1", "s1"]


def test_the_trace_root_carries_envelope_counters_freshness_and_truncation(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_trace, "CURSOR_DIR", tmp_path / "cursor")
    monkeypatch.setattr(correlation, "resolve", lambda session: {f"{P}session.id": session})
    monkeypatch.setattr("hooks.config.LANGFUSE_FIELD_MAX_CHARS", 3)
    signals.record({("sess-1", "events", "dropped"): 2})
    exporter = _Exporter()
    monkeypatch.setattr(otel, "langfuse_exporter", lambda: exporter)
    emitted = []
    monkeypatch.setattr(otel, "emit_event", lambda name, attrs: emitted.append((name, attrs)))
    monkeypatch.setattr(otel, "flush", lambda: None)
    path = _transcript(tmp_path, ENTRIES)

    agent_trace.export_session("sess-1", path, _identity())

    spans = exporter.batches[0]
    root = next(s for s in spans if s.parent is None).attributes
    truncated = sum(1 for s in spans for v in s.attributes.values() if isinstance(v, str) and "…[truncated " in v)
    assert root[f"{P}session.id"] == "sess-1"
    assert root["agentihooks.signals.events.dropped"] == 2
    assert root["agentihooks.export.last_accepted_at.state"] == correlation.MISSING
    assert root["agentihooks.export.queued.state"] == correlation.UNSUPPORTED
    assert root["agentihooks.export.generated_at"]
    assert root["agentihooks.export.spans"] == len(spans)
    assert root["agentihooks.export.truncated_fields"] == truncated > 0
    assert signals.read("sess-1")["traces.accepted"] == len(spans)
    assert signals.read("sess-1")["traces.truncated"] == truncated
    cursor = json.loads((tmp_path / "cursor" / "sess-1.json").read_text())
    assert cursor["turns"] == 2 and cursor["accepted_at"]

    agent_trace.export_session("sess-1", path, _identity())
    root = exporter.batches[1][0].attributes
    assert root["agentihooks.export.last_accepted_at"] == cursor["accepted_at"]


SUCCESS_AND_FAILURE = [
    ENTRIES[1],
    ENTRIES[3],
    ENTRIES[4],
    {
        "type": "assistant",
        "uuid": "a9",
        "timestamp": "2026-10-04T10:00:07.000Z",
        "message": {
            "id": "m9",
            "model": "claude-opus-5-5",
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tool-b", "name": "Read", "input": {}},
                {"type": "tool_use", "id": "tool-c", "name": "Grep", "input": {}},
            ],
            "usage": {},
        },
    },
    {
        "type": "user",
        "uuid": "r9",
        "timestamp": "2026-10-04T10:00:08.000Z",
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tool-b", "content": "fine"}]},
    },
]


@pytest.mark.parametrize(("accepted", "result"), [(True, "accepted"), (False, "failed")])
def test_each_tool_outcome_reaches_the_collector_joined_to_the_trace(monkeypatch, tmp_path, accepted, result):
    monkeypatch.setattr(agent_trace, "CURSOR_DIR", tmp_path / "cursor")
    exporter = _Exporter(result=accepted)
    monkeypatch.setattr(otel, "langfuse_exporter", lambda: exporter)
    emitted, flushed = [], []
    monkeypatch.setattr(otel, "emit_event", lambda name, attrs: emitted.append((name, attrs)))
    monkeypatch.setattr(otel, "flush", lambda: flushed.append(True))

    agent_trace.export_session("sess-1", _transcript(tmp_path, SUCCESS_AND_FAILURE), _identity())

    tools = {
        s.attributes["gen_ai.tool.call.id"]: s
        for s in exporter.batches[0]
        if s.attributes["langfuse.observation.type"] == "tool"
    }
    assert tools["tool-a"].attributes["tool.outcome"] == "error"
    assert tools["tool-b"].attributes["tool.outcome"] == "success"
    assert tools["tool-c"].attributes["tool.outcome.state"] == "missing"
    assert {s.context.trace_id for s in exporter.batches[0]} == {agent_trace.trace_id("sess-1")}
    events = {attrs["gen_ai.tool.call.id"]: attrs for name, attrs in emitted if name == "agentihooks.tool.outcome"}
    assert len(events) == len(emitted) == 3
    assert events["tool-a"]["tool.outcome"] == "error"
    assert events["tool-b"]["tool.outcome"] == "success"
    assert events["tool-c"]["tool.outcome.state"] == "missing"
    assert {attrs["session.id"] for attrs in events.values()} == {"sess-1"}
    assert {attrs["agentihooks.export.result"] for attrs in events.values()} == {result}
    assert events["tool-a"]["tool.started_at_ns"] == tools["tool-a"].start_time
    assert events["tool-a"]["tool.ended_at_ns"] == tools["tool-a"].end_time
    assert events["tool-a"]["gen_ai.tool.name"] == "Bash"
    assert flushed == [True]
    assert signals.read("sess-1")[f"traces.{result}"] == len(exporter.batches[0])
    assert (tmp_path / "cursor" / "sess-1.json").exists() is accepted


def test_a_collector_event_and_its_trace_share_one_trace_id(monkeypatch):
    monkeypatch.setattr(correlation, "gather", lambda s, e: _inputs(session_id=s))
    flat = correlation.resolve("sess-1", ENV)
    assert int(flat[f"{P}trace.id"], 16) == agent_trace.trace_id("sess-1")
