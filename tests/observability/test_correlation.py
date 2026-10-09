import fcntl
import json
import os
import queue

import pytest

from hooks.observability import agent_trace, correlation, otel, signals
from tests.observability.test_agent_trace import ENTRIES, _Exporter, _identity, _transcript

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

P = correlation.PREFIX
TOKEN_SENTINEL = "SENTINEL-not-a-real-token-value"

ENV = {
    "AGENTIHOOKS_SWARM": "rig",
    "AGENTIHOOKS_SWARM_TASK": "t44",
    "AGENTIHOOKS_AGENT_NAME": "engineer@1-2",
    "AGENTIHOOKS_PROFILE": "engineer",
    "AGENTIHOOKS_PREDECESSOR_SESSION": "sess-0",
    "AH_CC_TOKEN_probeacct": TOKEN_SENTINEL,
}


@pytest.fixture(autouse=True)
def _no_routed_shell(monkeypatch):
    for name in [n for n in os.environ if n.startswith("AH_CC_TOKEN_")] + list(correlation.KEYS):
        monkeypatch.delenv(name, raising=False)


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
        f"{P}account": "probeacct",
        f"{P}revision": "agentihooks-bundle@beef,agentihooks@f00d",
        f"{P}revision.sources": "abc123",
        f"{P}predecessor.session.id": "sess-0",
        f"{P}predecessor.trace.id": format(agent_trace.trace_id("sess-0"), "032x"),
    }
    assert set(correlation.SOURCES) == {key[len(P) :] for key in flat} - {"schema"}


def test_a_session_outside_any_swarm_marks_every_field_present_or_gapped():
    flat = _flat(session_id="", environ={}, harness="codex", agent=None, report=None, task=None)
    unsupported = {
        "ledger",
        "task",
        "phase",
        "seat",
        "agent.started_at",
        "agent.life",
        "conversation.id",
        "profile.validation",
        "profile.resolved",
        "profile.source",
        "classifier.model",
        "classifier.confidence",
        "model",
        "model.source",
        "model.confidence",
        "effort",
        "revision",
        "revision.sources",
    }
    expected = {f"{P}{name}.state": correlation.UNSUPPORTED for name in unsupported}
    missing = set(correlation.SOURCES) - unsupported - {"harness"}
    expected.update({f"{P}{name}.state": correlation.MISSING for name in missing})
    assert flat == {f"{P}schema": correlation.SCHEMA, f"{P}harness": "codex", **expected}
    assert missing == {
        "agent.name",
        "session.id",
        "trace.id",
        "profile.requested",
        "account",
        "predecessor.session.id",
        "predecessor.trace.id",
    }


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
    assert flat[f"{P}account"] == "probeacct"
    assert TOKEN_SENTINEL not in json.dumps(flat)


def test_gather_reads_the_record_ledger_task_and_report(monkeypatch, tmp_path):
    report = tmp_path / "report.json"
    report.write_text(json.dumps(REPORT))
    (tmp_path / "rig.json").write_text(json.dumps({"tasks": ["x", {"id": "t1"}, {"id": "t44", "phase": "p23"}]}))
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path))
    looked_up = []
    monkeypatch.setattr(correlation, "_agent_record", lambda slug, name: looked_up.append((slug, name)) or AGENT)
    env = {**ENV, "AGENTIHOOKS_PROFILE_REPORT": str(report), "AGENTIHOOKS_TARGET": "Codex "}

    inputs = correlation.gather("sess-1", env)

    assert looked_up == [("rig", "engineer@1-2")]
    assert inputs == correlation.Inputs("sess-1", env, "codex", AGENT, REPORT, {"id": "t44", "phase": "p23"})


def test_gather_in_a_swarm_without_name_task_or_report_looks_nothing_up(monkeypatch):
    monkeypatch.setattr(correlation, "_agent_record", lambda *a: pytest.fail("no agent lookup without a name"))
    monkeypatch.setattr(correlation, "_ledger_task", lambda *a: pytest.fail("no ledger read without a task"))
    inputs = correlation.gather("sess-1", {"AGENTIHOOKS_SWARM": "rig"})
    assert (inputs.agent, inputs.report, inputs.task) == ({}, None, {})


def test_ledger_task_defaults_to_the_home_ledger_folder_and_reads_gaps_as_empty(monkeypatch):
    from pathlib import Path

    monkeypatch.delenv("LEDGER_DIR", raising=False)
    folder = Path.home() / "development-ledger"
    folder.mkdir()
    (folder / "rig.json").write_text(json.dumps({"tasks": [{"id": "t44", "phase": "p23"}]}))
    (folder / "untasked.json").write_text(json.dumps({"title": "no tasks key"}))
    (folder / "odd.json").write_text(json.dumps({"tasks": None}))
    assert correlation._ledger_task("rig", "t44") == {"id": "t44", "phase": "p23"}
    assert correlation._ledger_task("rig", "t99") == {}
    assert correlation._ledger_task("untasked", "t44") == {}
    assert correlation._ledger_task("odd", "t44") == {}
    assert correlation._ledger_task("absent", "t44") == {}


def test_report_reads_a_json_object_or_nothing(tmp_path):
    (tmp_path / "list.json").write_text("[1]")
    (tmp_path / "bad.json").write_text("{")
    assert correlation._report(str(tmp_path / "list.json")) == {}
    assert correlation._report(str(tmp_path / "bad.json")) == {}
    assert correlation._report(str(tmp_path / "absent.json")) == {}


def test_gather_without_a_swarm_or_report_marks_those_sources_absent():
    inputs = correlation.gather("sess-1", {})
    assert (inputs.agent, inputs.report, inputs.task, inputs.harness) == (None, None, None, "claude")


def test_agent_record_lookup_failure_reads_as_an_empty_record():
    assert correlation._agent_record("rig", "engineer@1-2") == {}


def test_agent_record_reads_the_named_agent_from_the_swarm_store(monkeypatch):
    import fakeredis

    from scripts.swarm import store

    redis = fakeredis.FakeRedis(decode_responses=True)
    live = store.RedisStore(redis)
    redis.hset(live.key("rig", "agents"), mapping={"engineer@1-2": json.dumps(AGENT), "other": "{}"})
    monkeypatch.setattr(store, "connect", lambda: live)
    assert correlation._agent_record("rig", "engineer@1-2") == AGENT
    assert correlation._agent_record("rig", "absent") == {}
    assert correlation._agent_record("other-swarm", "engineer@1-2") == {}


def test_resolve_caches_per_session_until_the_ttl(monkeypatch):
    from hooks import config

    calls = []
    monkeypatch.setattr(correlation, "gather", lambda s, e: calls.append(s) or _inputs(session_id=s))
    now = [1000.0]
    monkeypatch.setattr(correlation.time, "time", lambda: now[0])
    first = correlation.resolve("sess/1", ENV)
    assert correlation.resolve("sess/1", ENV) == first
    assert calls == ["sess/1"]
    cached = json.loads((config.AGENTIHOOKS_HOME / "telemetry" / "correlation" / "sess_1.json").read_text())
    assert cached["attributes"] == first
    correlation.resolve("sess-2", ENV)
    assert calls == ["sess/1", "sess-2"]
    now[0] += correlation.CACHE_TTL_SEC - 0.5
    correlation.resolve("sess/1", ENV)
    assert calls == ["sess/1", "sess-2"]
    now[0] += 0.5
    correlation.resolve("sess/1", ENV)
    correlation.resolve("sess/1", ENV)
    assert calls == ["sess/1", "sess-2", "sess/1"]


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_validation_refreshes_traces_and_gauges_before_cache_expiry(monkeypatch, tmp_path, target):
    from scripts.profiles import binding

    monkeypatch.setattr(correlation.time, "time", lambda: 1000.0)
    report = tmp_path / "profile-report.json"
    binding.request(report, "engineer", target)
    monkeypatch.setenv("AGENTIHOOKS_PROFILE", "engineer")
    monkeypatch.setenv(binding.REPORT, str(report))
    monkeypatch.setenv("AGENTIHOOKS_TARGET", target)
    before = correlation.resolve("sess-1")
    assert before[f"{P}profile.validation"] == "pending"
    assert before[f"{P}profile.resolved.state"] == correlation.MISSING

    validated = {"profile": "engineer", "sources": "abc123", "revisions": {"/x/agentihooks": "f00d"}}
    env = {"AGENTIHOOKS_PROFILE": "engineer", binding.REPORT: str(report), binding.HOMES[target]: str(tmp_path)}
    monkeypatch.setattr(binding, "process", lambda: (123, target, env, "default"))
    monkeypatch.setattr(binding, "inspect", lambda *args: {**validated, "canary": "mounted-canary"})
    binding.validate("mounted-canary")

    gauge = _Gauge()
    monkeypatch.setattr(otel, "_meter", type("M", (), {"create_gauge": lambda self, name: gauge})())
    monkeypatch.setattr(otel, "_gauges", {})
    otel._dispatch_op(("gauge", "agentihooks.tokens.fill_pct", 25.0, {"session.id": "sess-1"}))
    after = gauge.points[0][1]
    assert after[f"{P}profile.validation"] == "validated"
    assert after[f"{P}profile.resolved"] == "engineer"
    assert after[f"{P}revision.sources"] == "abc123"
    assert after[f"{P}revision"] == "agentihooks@f00d"

    monkeypatch.setattr(agent_trace, "CURSOR_DIR", tmp_path / "cursor")
    exporter = _Exporter()
    monkeypatch.setattr(otel, "langfuse_exporter", lambda: exporter)
    monkeypatch.setattr(otel, "emit_event", lambda *args: None)
    monkeypatch.setattr(otel, "flush", lambda: None)
    agent_trace.export_session("sess-1", _transcript(tmp_path, ENTRIES), _identity())
    root = next(span for span in exporter.batches[0] if span.parent is None).attributes
    assert {key: value for key, value in root.items() if key.startswith(P)} == {
        key: value for key, value in after.items() if key.startswith(P)
    }


@pytest.mark.parametrize("change", ["revision", "failed", "pending", "deleted", "malformed"])
def test_changed_report_replaces_validated_evidence_without_expiring_cache(monkeypatch, tmp_path, change):
    monkeypatch.setattr(correlation.time, "time", lambda: 1000.0)
    report = tmp_path / "report.json"
    report.write_text(json.dumps(REPORT))
    env = {"AGENTIHOOKS_PROFILE": "engineer", "AGENTIHOOKS_PROFILE_REPORT": str(report)}
    first = correlation.resolve("validated-session", env)
    assert first[f"{P}profile.resolved"] == "engineer"
    unrelated = correlation.resolve("unrelated-session", {})
    if change == "deleted":
        report.unlink()
    elif change == "malformed":
        report.write_text("not json")
    elif change == "revision":
        report.write_text(
            json.dumps(
                {
                    **REPORT,
                    "validation": {
                        "profile": "engineer",
                        "sources": "new-source",
                        "revisions": {"/x/agentihooks": "new-revision"},
                    },
                }
            )
        )
    else:
        report.write_text(json.dumps({"state": change}))

    after = correlation.resolve("validated-session", env)
    if change == "revision":
        assert after[f"{P}profile.validation"] == "validated"
        assert after[f"{P}revision.sources"] == "new-source"
        assert after[f"{P}revision"] == "agentihooks@new-revision"
    else:
        if change in ("failed", "pending"):
            assert after[f"{P}profile.validation"] == change
        else:
            assert after[f"{P}profile.validation.state"] == correlation.MISSING
        for field in ("profile.resolved", "revision.sources", "revision"):
            assert after[f"{P}{field}.state"] == correlation.MISSING
            assert f"{P}{field}" not in after
    assert correlation.resolve("unrelated-session", {}) == unrelated


@pytest.mark.parametrize("key", [*correlation.KEYS, "AH_CC_TOKEN_other"])
def test_a_changed_launch_environment_recomputes_the_envelope(monkeypatch, key):
    calls = []
    monkeypatch.setattr(correlation, "gather", lambda s, e: calls.append(dict(e)) or _inputs(session_id=s))
    correlation.resolve("sess-1", ENV)
    changed = {k: v for k, v in ENV.items() if not k.startswith("AH_CC_TOKEN_")} if key.startswith("AH_") else ENV
    correlation.resolve("sess-1", {**changed, key: "changed"})
    assert len(calls) == 2


def test_an_unwritable_cache_still_returns_the_envelope(monkeypatch):
    from hooks import config

    monkeypatch.setattr(correlation, "gather", lambda s, e: _inputs(session_id=s))
    (config.AGENTIHOOKS_HOME / "telemetry").write_text("a file where the folder belongs")
    assert correlation.resolve("sess-1", ENV)[f"{P}session.id"] == "sess-1"


def test_signal_counts_accumulate_per_session():
    from hooks import config

    signals.record({("s/1", "events", "queued"): 2, ("s/1", "events", "dropped"): 1, ("", "gauges", "queued"): 1})
    signals.record({("s/1", "events", "queued"): 3, ("s/1", "events", "unsupported"): 0})
    signals.record({("unattributed", "gauges", "queued"): 2, ("", "gauges", "queued"): 4})
    assert signals.read("s/1") == {"events.queued": 5, "events.dropped": 1}
    assert signals.attributes("s/1") == {
        "agentihooks.signals.events.dropped": 1,
        "agentihooks.signals.events.queued": 5,
    }
    assert signals.read("") == {"gauges.queued": 7}
    folder = config.AGENTIHOOKS_HOME / "telemetry" / "signals"
    assert sorted(p.name for p in folder.iterdir()) == ["s_1.json", "unattributed.json"]


def test_one_unwritable_session_does_not_stop_the_others():
    from hooks import config

    (config.AGENTIHOOKS_HOME / "telemetry" / "signals" / "bad.json").mkdir(parents=True)
    signals.record({("bad", "events", "queued"): 1, ("good", "events", "queued"): 1})
    assert signals.read("good") == {"events.queued": 1}
    assert signals.read("bad") == {}


def test_counts_are_added_under_an_exclusive_folder_lock(monkeypatch):
    locks = []
    monkeypatch.setattr(signals.fcntl, "flock", lambda fd, op: locks.append(op))
    signals.record({("s1", "events", "queued"): 1})
    assert locks == [fcntl.LOCK_EX]


def test_a_non_object_signal_file_reads_empty():
    from hooks import config

    folder = config.AGENTIHOOKS_HOME / "telemetry" / "signals"
    folder.mkdir(parents=True)
    (folder / "s1.json").write_text("[1]")
    (folder / "s2.json").write_text('{"events.queued": 2, "note": "x"}')
    assert signals.read("s1") == {}
    assert signals.read("s2") == {"events.queued": 2}


def test_state_files_are_staged_beside_their_target_so_the_rename_stays_on_one_filesystem(monkeypatch):
    from hooks import config

    real, folders = signals.tempfile.mkstemp, []
    monkeypatch.setattr(signals.tempfile, "mkstemp", lambda dir: folders.append(dir) or real(dir=dir))
    monkeypatch.setattr(correlation, "gather", lambda s, e: _inputs(session_id=s))
    signals.record({("s1", "events", "queued"): 1})
    correlation.resolve("s1", ENV)
    telemetry = config.AGENTIHOOKS_HOME / "telemetry"
    assert folders == [telemetry / "signals", telemetry / "correlation"]
    assert sorted(p.name for p in (telemetry / "signals").iterdir()) == ["s1.json"]


def test_safe_names_keep_only_portable_characters():
    assert signals.safe_name("abc-DEF_09") == "abc-DEF_09"
    assert signals.safe_name("a/b c.é") == "a_b_c__"
    assert signals.safe_name("") == signals.UNATTRIBUTED


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
    otel.record_gauge("g", 1.0, {})
    assert otel._counts == {("s1", "events", "unsupported"): 1, ("", "gauges", "unsupported"): 1}


def test_queued_signals_reach_the_worker_unchanged_and_are_counted(worker, monkeypatch):
    monkeypatch.setattr(otel, "_q", queue.Queue(maxsize=3))
    otel.emit_event("e", {"session.id": "s1"})
    otel.emit_event("f", {"session.id": "s1"})
    otel.record_gauge("g", 2, {})
    assert [otel._q.get_nowait() for _ in range(3)] == [
        ("event", "e", {"session.id": "s1"}),
        ("event", "f", {"session.id": "s1"}),
        ("gauge", "g", 2.0, {}),
    ]
    otel.flush()
    assert signals.read("s1") == {"events.queued": 2}
    assert signals.read(signals.UNATTRIBUTED) == {"gauges.queued": 1}


@pytest.fixture
def drain_env(monkeypatch):
    from hooks import config

    monkeypatch.setenv("AGENTIHOOKS_OTLP_ENDPOINT", "http://collector:4318")
    folder = config.AGENTIHOOKS_HOME / "telemetry"
    folder.mkdir(parents=True, exist_ok=True)
    states = list(folder.glob("flush-*"))
    assert not states
    return folder


def _state(folder):
    import hashlib

    key = hashlib.sha256(b"http://collector:4318|").hexdigest()
    return folder / f"flush-{key}"


def test_drain_without_a_collector_reports_the_worker_flush(monkeypatch):
    monkeypatch.setattr(otel, "_flush_pending", lambda: True)
    assert otel._drain() is True
    monkeypatch.setattr(otel, "_flush_pending", lambda: False)
    assert otel._drain() is False


def test_drain_confirms_a_flush_and_clears_the_cooldown(drain_env, monkeypatch):
    _state(drain_env).touch()
    os.utime(_state(drain_env), (0, 0))
    monkeypatch.setattr(otel, "_flush_pending", lambda: True)
    assert otel._drain() is True
    assert not _state(drain_env).exists()


def test_drain_inside_the_cooldown_is_unconfirmed(drain_env, monkeypatch):
    _state(drain_env).touch()
    monkeypatch.setattr(otel, "_flush_pending", lambda: pytest.fail("cooldown skips the flush"))
    assert otel._drain() is False


def test_a_failed_drain_starts_the_cooldown(drain_env, monkeypatch):
    monkeypatch.setattr(otel, "_flush_pending", lambda: False)
    assert otel._drain() is False
    assert _state(drain_env).exists()


def test_a_drain_racing_another_failure_is_unconfirmed(drain_env, monkeypatch):
    def other_failed():
        _state(drain_env).touch()
        return False

    monkeypatch.setattr(otel, "_flush_pending", other_failed)
    assert otel._drain() is False


def test_a_drain_while_another_holds_the_lock_is_unconfirmed(drain_env, monkeypatch):
    monkeypatch.setattr(otel, "_flush_pending", lambda: False)
    with _state(drain_env).with_suffix(".lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert otel._drain() is False
    assert not _state(drain_env).exists()


def test_a_drain_that_cannot_write_its_state_is_unconfirmed(monkeypatch):
    from hooks import config

    monkeypatch.setenv("AGENTIHOOKS_OTLP_ENDPOINT", "http://collector:4318")
    (config.AGENTIHOOKS_HOME / "telemetry").write_text("a file where the folder belongs")
    monkeypatch.setattr(otel, "_flush_pending", lambda: False)
    assert otel._drain() is False


def test_a_worker_event_without_a_session_resolves_the_unattributed_envelope(monkeypatch):
    seen = []
    monkeypatch.setattr(correlation, "resolve", lambda session: seen.append(session) or {})
    otel._correlation({})
    otel._correlation({"session.id": None})
    assert seen == ["", ""]


def test_flush_without_a_worker_writes_nothing(monkeypatch):
    monkeypatch.setattr(otel, "_q", None)
    monkeypatch.setattr(otel, "_counts", {("s1", "events", "unsupported"): 1})
    otel.flush()
    assert signals.read("s1") == {}


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
    monkeypatch.setattr("hooks.context.context_usage.session_cost", lambda s: 2.5 if s == "sess-1" else None)
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
    assert root["agentihooks.export.generated_at"].endswith("+00:00")
    assert root["gen_ai.usage.cost"] == 2.5
    assert root["agentihooks.export.spans"] == len(spans)
    assert root["agentihooks.export.truncated_fields"] == truncated > 0
    assert signals.read("sess-1")["traces.accepted"] == len(spans)
    assert signals.read("sess-1")["traces.truncated"] == truncated
    cursor = json.loads((tmp_path / "cursor" / "sess-1.json").read_text())
    assert cursor["turns"] == 2 and cursor["accepted_at"].endswith("+00:00")

    agent_trace.export_session("sess-1", path, _identity())
    assert len(exporter.batches) == 1
    assert agent_trace._root_attributes("sess-1")["agentihooks.export.last_accepted_at"] == cursor["accepted_at"]


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


@pytest.mark.parametrize(("accepted", "result"), [(True, "accepted"), (False, "unconfirmed")])
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
    assert bool(agent_trace._cursor("sess-1")["accepted"]) is accepted


def test_a_collector_event_and_its_trace_share_one_trace_id(monkeypatch):
    monkeypatch.setattr(correlation, "gather", lambda s, e: _inputs(session_id=s))
    flat = correlation.resolve("sess-1", ENV)
    assert int(flat[f"{P}trace.id"], 16) == agent_trace.trace_id("sess-1")
