import json
import threading
from dataclasses import asdict

import pytest
from opentelemetry.sdk.trace.export import SpanExportResult

from hooks.observability import agent_trace, otel


class Receiver:
    def __init__(self):
        self.calls = []
        self.observations = {}
        self.results = []

    def export(self, spans):
        self.calls.append(spans)
        result = self.results.pop(0) if self.results else SpanExportResult.SUCCESS
        if result != SpanExportResult.FAILURE:
            self.observations.update({s.context.span_id: s for s in spans})
        if isinstance(result, Exception):
            raise result
        return result

    def shutdown(self):
        pass


@pytest.fixture
def export(tmp_path, monkeypatch):
    receiver = Receiver()
    monkeypatch.setattr(agent_trace, "CURSOR_DIR", tmp_path / "cursor")
    monkeypatch.setattr(otel, "langfuse_exporter", lambda: receiver)
    monkeypatch.setattr(agent_trace, "_root_attributes", lambda session: {})
    monkeypatch.setattr(agent_trace, "_collector_outcomes", lambda *args: None)
    return receiver


@pytest.fixture(params=["claude", "codex"])
def transcript(request, tmp_path):
    stamp = "2026-10-07T10:00:00Z"
    if request.param == "claude":
        records = [
            {"type": "user", "uuid": "prompt", "timestamp": stamp, "message": {"content": "probe"}},
            {
                "type": "assistant",
                "uuid": "call",
                "timestamp": stamp,
                "message": {
                    "id": "generation",
                    "model": "model",
                    "content": [{"type": "tool_use", "id": "tool", "name": "shell", "input": {"cmd": "probe"}}],
                },
            },
            {
                "type": "user",
                "uuid": "result",
                "timestamp": stamp,
                "message": {"content": [{"type": "tool_result", "tool_use_id": "tool", "content": "late result"}]},
            },
        ]
    else:
        records = [
            {"type": "session_meta", "timestamp": stamp, "payload": {"id": "session"}},
            {"type": "turn_context", "timestamp": stamp, "payload": {"model": "model"}},
            {
                "type": "response_item",
                "timestamp": stamp,
                "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "probe"}]},
            },
            {
                "type": "response_item",
                "timestamp": stamp,
                "payload": {
                    "type": "function_call",
                    "call_id": "tool",
                    "name": "shell",
                    "arguments": '{"cmd":"probe"}',
                },
            },
            {
                "type": "response_item",
                "timestamp": stamp,
                "payload": {"type": "function_call_output", "call_id": "tool", "output": "late result"},
            },
        ]
    path = tmp_path / "transcript.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records[:-1]))
    return path, records


def flush(path):
    agent_trace.export_session("session", str(path), agent_trace.Identity("session"))


def test_open_turn_accepts_late_result_once(export, transcript):
    path, records = transcript
    flush(path)
    tool_id = agent_trace._span_id("session", "tool")
    assert tool_id in export.observations
    with path.open("a") as handle:
        handle.write(json.dumps(records[-1]) + "\n")
    flush(path)
    assert export.observations[tool_id].attributes["langfuse.observation.output"] == "late result"
    logical = set(export.observations)
    calls = len(export.calls)
    flush(path)
    assert set(export.observations) == logical
    assert len(export.calls) == calls


def test_partial_valid_json_line_waits(export, transcript):
    path, records = transcript
    with path.open("a") as handle:
        handle.write(json.dumps(records[-1]))
    flush(path)
    tool_id = agent_trace._span_id("session", "tool")
    assert "langfuse.observation.output" not in export.observations[tool_id].attributes
    with path.open("a") as handle:
        handle.write("\n")
    flush(path)
    assert export.observations[tool_id].attributes["langfuse.observation.output"] == "late result"


def test_lost_ack_retries_identical_observations(export, transcript):
    path, _ = transcript
    export.results = [TimeoutError("lost acknowledgement")]
    flush(path)
    state = agent_trace._cursor("session")
    assert not state.get("accepted")
    first = [(s.context.span_id, dict(s.attributes)) for s in export.calls[0]]
    flush(path)
    assert [(s.context.span_id, dict(s.attributes)) for s in export.calls[1]] == first
    assert len(export.observations) == len(first)


def test_partial_batch_commits_only_accepted_records(export, transcript, monkeypatch):
    path, _ = transcript
    monkeypatch.setattr(agent_trace, "BATCH_CHARS", 1)
    export.results = [SpanExportResult.SUCCESS, SpanExportResult.FAILURE]
    flush(path)
    accepted_id = export.calls[0][0].context.span_id
    assert set(agent_trace._cursor("session")["accepted"]) == {f"{accepted_id:016x}"}
    before = len(export.calls)
    flush(path)
    assert accepted_id not in {s.context.span_id for call in export.calls[before:] for s in call}


def test_rotation_compaction_and_resume_keep_logical_identity(export, transcript):
    path, records = transcript
    flush(path)
    logical = set(export.observations)
    path.rename(path.with_suffix(".old"))
    path.write_text(json.dumps(records[-1]) + "\n")
    flush(path)
    assert set(export.observations) == logical
    tool_id = agent_trace._span_id("session", "tool")
    assert export.observations[tool_id].attributes["langfuse.observation.output"] == "late result"
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    calls = len(export.calls)
    flush(path)
    assert len(export.calls) == calls


def test_cursor_contains_only_masked_io(export, transcript):
    path, records = transcript
    planted = "ghp_" + "Q7" * 18
    records[-1] = json.loads(json.dumps(records[-1]).replace("late result", planted))
    with path.open("a") as handle:
        handle.write(json.dumps(records[-1]) + "\n")
    export.results = [SpanExportResult.FAILURE]
    flush(path)
    durable = agent_trace._cursor_path("session").read_text()
    assert planted not in durable
    assert "REDACTED" in durable


def test_concurrent_flush_serializes_acceptance(export, transcript):
    path, _ = transcript
    threads = [threading.Thread(target=flush, args=(path,)) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(export.calls) == 1
    state = agent_trace._cursor("session")
    assert state["version"] == 2
    assert len(state["accepted"]) == len(export.observations)


def test_span_serialization_keeps_fields():
    spec = agent_trace.SpanSpec("name", 1, 2, 3, 4, {"value": 5})
    assert agent_trace.SpanSpec(**asdict(spec)) == spec


def test_acknowledged_source_does_not_jump_over_late_pending_result(export, transcript):
    path, records = transcript
    export.results = [SpanExportResult.FAILURE]
    flush(path)
    old_bytes = path.stat().st_size
    with path.open("a") as handle:
        handle.write(json.dumps(records[-1]) + "\n")
    flush(path)
    assert agent_trace._cursor("session")["source"]["accepted_bytes"] == old_bytes
    flush(path)
    assert agent_trace._cursor("session")["source"]["accepted_bytes"] == path.stat().st_size
    tool = export.observations[agent_trace._span_id("session", "tool")]
    assert tool.attributes["langfuse.observation.output"] == "late result"


def test_overflow_keeps_source_retryable(export, transcript, monkeypatch, capsys):
    path, _ = transcript
    monkeypatch.setattr(agent_trace, "PENDING_MAX_BYTES", 1)
    flush(path)
    state = agent_trace._cursor("session")
    assert state["overflow"]["bytes"] > state["overflow"]["limit"] == 1
    assert not state["accepted"] and not state["records"]
    assert state["source"].get("accepted_bytes", 0) == 0
    assert "overflow" in capsys.readouterr().err
    monkeypatch.setattr(agent_trace, "PENDING_MAX_BYTES", 8_000_000)
    flush(path)
    assert agent_trace._cursor("session")["accepted"]


def test_unknown_progress_version_is_preserved(export, transcript, capsys):
    path, _ = transcript
    cursor = agent_trace._cursor_path("session")
    cursor.parent.mkdir()
    cursor.write_text('{"version": 99}')
    flush(path)
    assert cursor.read_text() == '{"version": 99}'
    assert not export.calls
    assert "unsupported exporter progress version" in capsys.readouterr().err


def test_pending_payload_is_masked_before_truncation(export, transcript, monkeypatch):
    path, records = transcript
    planted = "ghp_" + "Q7" * 18
    records[-1] = json.loads(json.dumps(records[-1]).replace("late result", planted + " " + "x" * 30))
    with path.open("a") as handle:
        handle.write(json.dumps(records[-1]) + "\n")
    monkeypatch.setattr("hooks.config.LANGFUSE_FIELD_MAX_CHARS", 5)
    flush(path)
    tool = export.observations[agent_trace._span_id("session", "tool")]
    output = tool.attributes["langfuse.observation.output"]
    assert output.startswith("[REDA") and planted not in output
    assert (
        tool.attributes["agentihooks.truncation.langfuse.observation.output.chars"]
        == len("[REDACTED:github_token]" + " " + "x" * 30) - 5
    )


@pytest.mark.parametrize(
    "body,content_type,accepted",
    [
        (b"", "application/x-protobuf", True),
        (b'{"partialSuccess":{"rejectedSpans":"1"}}', "application/json", False),
        (b'{"partial_success":{"rejected_spans":1}}', "application/json", False),
        (b'{"partialSuccess":{"rejectedSpans":"0","errorMessage":"warning"}}', "application/json", True),
        (b'{"jobId":"durably-queued"}', "application/json", True),
        (b"invalid", "application/json", None),
    ],
)
def test_http_acknowledgement_checks_rejected_spans(body, content_type, accepted, monkeypatch):
    import requests
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    exporter = OTLPSpanExporter(endpoint="http://localhost:1")
    response = requests.Response()
    response.status_code = 200
    response._content = body
    response.headers["Content-Type"] = content_type
    sent = []
    monkeypatch.setattr(otel, "langfuse_exporter_config", lambda: {"endpoint": "http://localhost:1", "headers": {}})
    monkeypatch.setattr(
        requests, "post", lambda endpoint, data, headers, timeout: sent.append((data, timeout)) or response
    )
    spec = agent_trace.SpanSpec("probe", 1, None, 1, 2, {"value": 3})
    if accepted is None:
        with pytest.raises(ValueError):
            agent_trace._batch_accepted(exporter, [spec], 1)
    else:
        assert agent_trace._batch_accepted(exporter, [spec], 1) is accepted
    assert len(sent) == 1 and sent[0][1] == otel.LANGFUSE_EXPORT_TIMEOUT_SEC
    exporter.shutdown()


def test_protobuf_partial_acknowledgement_keeps_records_retryable(monkeypatch):
    import requests
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse

    exporter = OTLPSpanExporter(endpoint="http://localhost:1")
    acknowledgement = ExportTraceServiceResponse()
    acknowledgement.partial_success.rejected_spans = 1
    response = requests.Response()
    response.status_code = 200
    response._content = acknowledgement.SerializeToString()
    response.headers["Content-Type"] = "application/x-protobuf"
    monkeypatch.setattr(otel, "langfuse_exporter_config", lambda: {"endpoint": "http://localhost:1", "headers": {}})
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: response)
    assert agent_trace._batch_accepted(exporter, [agent_trace.SpanSpec("probe", 1, None, 1, 2)], 1) is False
    exporter.shutdown()


def test_planted_replay_fault_is_detected(export, transcript, monkeypatch):
    counter = iter(range(1, 1000))
    monkeypatch.setattr(agent_trace, "_span_id", lambda *args: next(counter))
    with pytest.raises(AssertionError):
        test_rotation_compaction_and_resume_keep_logical_identity(export, transcript)


def test_planted_redaction_fault_is_detected(export, transcript, monkeypatch):
    from hooks.observability import transcript as source

    monkeypatch.setattr(source, "mask_value", lambda value: value)
    with pytest.raises(AssertionError):
        test_cursor_contains_only_masked_io(export, transcript)


def test_complete_source_reports_invalid_records_and_retains_partial_line(tmp_path):
    from hooks.observability import transcript as source

    path = tmp_path / "source.jsonl"
    data = b'bad\n[]\n\xff\n{"uuid":"valid"}\n{"uuid":"partial"}'
    path.write_bytes(data)
    records, position, unsupported = source.complete_records(str(path))
    assert records == [{"uuid": "valid"}]
    assert position == len(data) - len(b'{"uuid":"partial"}')
    assert unsupported == 3


def test_mask_value_preserves_json_types_and_masks_nested_values():
    from hooks.observability import transcript as source

    planted = "ghp_" + "Q7" * 18
    value = {planted: [True, None, 4, {"content": planted}], "tuple": (planted,)}
    assert source.mask_value(value) == {
        "[REDACTED:github_token]": [True, None, 4, {"content": "[REDACTED:github_token]"}],
        "tuple": ["[REDACTED:github_token]"],
    }


def test_unsupported_native_io_is_counted(export, transcript):
    path, records = transcript
    if records[0]["type"] == "session_meta":
        unsupported = {"type": "response_item", "timestamp": records[0]["timestamp"], "payload": {"type": "reasoning"}}
    else:
        unsupported = {
            "type": "assistant",
            "uuid": "unsupported",
            "timestamp": records[0]["timestamp"],
            "message": {"content": [{"type": "audio", "data": "unsupported"}]},
        }
    with path.open("a") as handle:
        handle.write(json.dumps(unsupported) + "\ninvalid complete record\n")
    flush(path)
    root = next(span for span in export.observations.values() if span.parent is None)
    assert root.attributes["agentihooks.export.unsupported_io"] == 1
    assert root.attributes["agentihooks.export.unsupported_records"] == 1
    assert root.attributes["agentihooks.export.replay_contract"] == "legacy-observations"
    assert root.attributes["agentihooks.export.v4_replay.state"] == "unsupported"


def test_late_result_after_next_prompt_updates_original_tool(export, transcript):
    path, records = transcript
    flush(path)
    prompt = json.loads(json.dumps(records[-2]))
    if prompt["type"] == "assistant":
        prompt = {"type": "user", "uuid": "next", "timestamp": records[0]["timestamp"], "message": {"content": "next"}}
    else:
        prompt["payload"] = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "next"}]}
    with path.open("a") as handle:
        handle.write(json.dumps(prompt) + "\n" + json.dumps(records[-1]) + "\n")
    flush(path)
    tool = export.observations[agent_trace._span_id("session", "tool")]
    assert tool.attributes["langfuse.observation.output"] == "late result"


def test_acknowledgement_after_atomic_save_failure_replays_same_batch(export, transcript, monkeypatch):
    path, _ = transcript
    save = agent_trace._save_progress
    saves = 0

    def fail_on_ack(session, state):
        nonlocal saves
        saves += 1
        if saves == 2:
            raise OSError("controlled atomic failure")
        save(session, state)

    monkeypatch.setattr(agent_trace, "_save_progress", fail_on_ack)
    flush(path)
    first = [(s.context.span_id, dict(s.attributes)) for s in export.calls[0]]
    assert not agent_trace._cursor("session")["accepted"]
    monkeypatch.setattr(agent_trace, "_save_progress", save)
    flush(path)
    assert [(s.context.span_id, dict(s.attributes)) for s in export.calls[1]] == first
    assert len(export.observations) == len(first)
    assert agent_trace._cursor_path("session").stat().st_mode & 0o777 == 0o600


def test_legacy_cursor_migrates_codex_identity(export, transcript):
    path, records = transcript
    if records[0]["type"] != "session_meta":
        pytest.skip("legacy positional identities are Codex specific")
    cursor = agent_trace._cursor_path("session")
    cursor.parent.mkdir()
    cursor.write_text('{"turns": 1, "accepted_at": "legacy"}')
    flush(path)
    generation = next(
        s for s in export.observations.values() if s.attributes["langfuse.observation.type"] == "generation"
    )
    assert generation.context.span_id == agent_trace._span_id("session", "codex-3")
    turn = next(s for s in export.observations.values() if s.name == "turn 1")
    assert turn.context.span_id == agent_trace._span_id("session", "codex-2")
    path.write_text(json.dumps(records[-1]) + "\n")
    flush(path)
    assert generation.context.span_id in export.observations
    assert (
        export.observations[agent_trace._span_id("session", "tool")].attributes["langfuse.observation.output"]
        == "late result"
    )


def test_new_unsupported_input_updates_accepted_root(export, transcript):
    path, records = transcript
    flush(path)
    if records[0]["type"] == "session_meta":
        unsupported = {"type": "response_item", "timestamp": records[0]["timestamp"], "payload": {"type": "reasoning"}}
    else:
        unsupported = {
            "type": "assistant",
            "uuid": "new-unsupported",
            "timestamp": records[0]["timestamp"],
            "message": {"content": [{"type": "audio", "data": "unsupported"}]},
        }
    with path.open("a") as handle:
        handle.write(json.dumps(unsupported) + "\ninvalid complete record\n")
    flush(path)
    root = next(span for span in export.observations.values() if span.parent is None)
    assert root.attributes["agentihooks.export.unsupported_io"] == 1
    assert root.attributes["agentihooks.export.unsupported_records"] == 1
    assert len(export.calls) == 2


def test_growing_source_during_retry_respects_combined_budget(export, transcript, monkeypatch):
    path, records = transcript
    monkeypatch.setattr(agent_trace, "PENDING_MAX_BYTES", 5000)
    export.results = [SpanExportResult.FAILURE, SpanExportResult.FAILURE]
    flush(path)
    before = agent_trace._cursor("session")
    assert before["pending"]
    extra = {"type": "ignored", "uuid": "oversized", "content": "x" * 4000}
    with path.open("a") as handle:
        handle.write(json.dumps(extra) + "\n")
    flush(path)
    state = agent_trace._cursor("session")
    assert state["overflow"]["bytes"] > 5000
    assert state["records"] == before["records"]
    size = len(json.dumps({"records": state["records"], "pending": state["pending"]}, ensure_ascii=False).encode())
    assert size <= 5000


def test_literal_truncation_marker_is_supported_output(export, transcript):
    path, records = transcript
    literal = "literal …[truncated 123 chars]"
    records[-1] = json.loads(json.dumps(records[-1]).replace("late result", literal))
    with path.open("a") as handle:
        handle.write(json.dumps(records[-1]) + "\n")
    flush(path)
    tool = export.observations[agent_trace._span_id("session", "tool")]
    assert tool.attributes["langfuse.observation.output"] == literal
    assert "agentihooks.truncation.langfuse.observation.output.chars" not in tool.attributes


def test_signal_only_change_updates_root_without_export_feedback(export, transcript, monkeypatch):
    path, _ = transcript
    counter = {"agentihooks.signals.logs.accepted": 1, "agentihooks.signals.traces.updated": 0}
    monkeypatch.setattr(agent_trace, "_root_attributes", lambda session: dict(counter))
    flush(path)
    counter["agentihooks.signals.logs.accepted"] = 2
    flush(path)
    root = next(span for span in export.observations.values() if span.parent is None)
    assert root.attributes["agentihooks.signals.logs.accepted"] == 2
    assert len(export.calls) == 2
    counter["agentihooks.signals.traces.updated"] = 1
    flush(path)
    assert len(export.calls) == 2


def test_accepted_history_does_not_exhaust_pending_budget(export, transcript, monkeypatch):
    path, records = transcript
    monkeypatch.setattr("hooks.config.LANGFUSE_FIELD_MAX_CHARS", 5)
    monkeypatch.setattr(agent_trace, "PENDING_MAX_BYTES", 5000)
    for index in range(8):
        if records[0]["type"] == "session_meta":
            record = {
                "type": "response_item",
                "timestamp": f"2026-10-07T10:00:{index:02d}Z",
                "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "x" * 600}]},
            }
        else:
            record = {
                "type": "user",
                "uuid": f"prompt-{index}",
                "timestamp": f"2026-10-07T10:00:{index:02d}Z",
                "message": {"content": "x" * 600},
            }
        path.write_text(json.dumps(record) + "\n")
        flush(path)
        state = agent_trace._cursor("session")
        assert "overflow" not in state and not state["pending"]
        assert state["turns"] == index + 1
    assert len(json.dumps(state["records"])) > 5000
    assert agent_trace._pending_bytes(state, state["records"], state["pending"]) == len(
        '{"records": {}, "pending": []}'
    )


def test_progress_schema_and_native_source_positions(export, transcript):
    from hooks.observability import transcript as source

    path, records = transcript
    assert agent_trace._progress("session") == {
        "version": 2,
        "records": {},
        "accepted": {},
        "pending": [],
        "source": {},
    }
    export.results = [SpanExportResult.FAILURE]
    flush(path)
    state = agent_trace._cursor("session")
    stat = path.stat()
    assert state["session_id"] == "session"
    assert state["source"] == {
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "buffered_bytes": stat.st_size,
        "records": len(records) - 1,
        "accepted_bytes": 0,
    }
    for key, record in state["records"].items():
        assert record["_source_id"] == key == source.record_id({k: v for k, v in record.items() if k != "_source_id"})
    flush(path)
    assert agent_trace._cursor("session")["source"]["accepted_bytes"] == stat.st_size
    with path.open("a") as handle:
        handle.write(json.dumps(records[-1]) + "\n")
    export.results = [SpanExportResult.FAILURE]
    flush(path)
    assert agent_trace._cursor("session")["source"]["accepted_bytes"] == stat.st_size


def test_empty_legacy_progress_keeps_explicit_schema(export):
    path = agent_trace._cursor_path("session")
    path.parent.mkdir()
    path.write_text("{}")
    assert agent_trace._progress("session") == {
        "version": 2,
        "records": {},
        "accepted": {},
        "pending": [],
        "source": {},
        "legacy_turns": 0,
    }
    path.write_text('{"turns":2}')
    assert agent_trace._progress("session")["legacy_turns"] == 2
    path.write_text('{"version":99}')
    with pytest.raises(ValueError, match="^unsupported exporter progress version$"):
        agent_trace._progress("session")


def test_source_identity_is_canonical_and_native_uuid_wins():
    import hashlib

    from hooks.observability import transcript as source

    assert (
        source.record_id({"uuid": "native", "text": "before"})
        == source.record_id({"uuid": "native", "text": "after"})
        == "native"
    )
    record = {"text": "ñ", "a": 1}
    expected = hashlib.sha256('{"a":1,"text":"ñ"}'.encode()).hexdigest()
    assert source.record_id(record) == source.record_id({"a": 1, "text": "ñ"}) == expected


@pytest.mark.parametrize("kind", ["session_meta", "turn_context", "response_item"])
def test_codex_source_detection_without_other_record_types(kind, monkeypatch):
    from hooks.observability import codex_transcript

    seen = []
    monkeypatch.setattr(codex_transcript, "normalize_entries", lambda entries: seen.extend(entries) or ["normalized"])
    entries = [{"type": kind}]
    assert agent_trace._normalize(entries) == ["normalized"]
    assert seen == entries


def test_codex_normalized_ids_use_native_generation_and_durable_source():
    from hooks.observability import codex_transcript

    record = {
        "type": "response_item",
        "_source_id": "durable",
        "payload": {"type": "message", "role": "assistant", "id": "native", "content": [{"text": "probe"}]},
    }
    assert codex_transcript.normalize_entries([record]) == [
        {
            "type": "assistant",
            "uuid": "codex-durable",
            "timestamp": "",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "probe"}],
                "id": "native",
                "model": "",
            },
        }
    ]


def test_mask_value_is_strict_even_when_operator_mode_is_off(monkeypatch):
    from hooks.observability import transcript as source

    monkeypatch.setattr("hooks.config.SECRETS_MODE", "off")
    planted = "sk_" + "live_" + "Q7" * 18
    masked = source.mask_value({planted: planted, "nested": '{"token":"' + planted + '"}', "text": "ñ"})
    assert planted not in json.dumps(masked)
    assert "[REDACTED:stripe_key]" in json.dumps(masked)
    assert masked["text"] == "ñ"


def test_overflow_reports_exact_signal_and_size(monkeypatch, capsys):
    from hooks.observability import signals

    calls = []
    monkeypatch.setattr(signals, "record", lambda values: calls.append(values))
    state = {"session_id": "session", "overflow": {"bytes": 10, "limit": 5}}
    agent_trace._report_progress(state, "overflow")
    assert calls == [{("session", "traces", "overflow"): 1}]
    assert capsys.readouterr().err == 'agent trace export overflow: {"bytes": 10, "limit": 5}\n'
    agent_trace._report_progress({}, "overflow")
    assert calls[-1] == {("", "traces", "overflow"): 1}
    assert capsys.readouterr().err == "agent trace export overflow: {}\n"


def test_atomic_progress_uses_same_filesystem_and_syncs(export, monkeypatch):
    import tempfile

    cursor = agent_trace._cursor_path("session")
    cursor.parent.mkdir()
    create = tempfile.NamedTemporaryFile
    sync = agent_trace.os.fsync
    created = []
    synced = []

    def temporary(**kwargs):
        created.append(kwargs)
        return create(**kwargs)

    def fsync(fd):
        synced.append(fd)
        return sync(fd)

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", temporary)
    monkeypatch.setattr(agent_trace.os, "fsync", fsync)
    agent_trace._save_progress("session", {"text": "ñ"})
    assert created == [{"mode": "w", "dir": cursor.parent, "delete": False}]
    assert len(synced) == 2
    assert cursor.read_text() == '{"text": "ñ"}'
    assert list(cursor.parent.iterdir()) == [cursor]


def test_nested_cursor_parent_and_lock_are_created_private(export, transcript, monkeypatch):
    path, _ = transcript
    nested = agent_trace.CURSOR_DIR / "nested" / "deep"
    monkeypatch.setattr(agent_trace, "CURSOR_DIR", nested)
    flush(path)
    lock = agent_trace._cursor_path("session").with_suffix(".lock")
    assert lock.exists() and lock.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "kind", ["message", "function_call", "custom_tool_call", "function_call_output", "custom_tool_call_output"]
)
def test_supported_codex_io_is_not_counted_unsupported(kind):
    assert agent_trace._unsupported_io([{"type": "response_item", "payload": {"type": kind}}], []) == 0


def test_omitted_codex_and_invalid_claude_blocks_are_counted_exactly():
    records = [
        {
            "type": "response_item",
            "payload": {"type": "message", "content": [{"type": "image"}, {"text": "supported"}]},
        },
        {"type": "response_item"},
        {"type": "ignored", "payload": {"type": "message", "content": [{"type": "image"}]}},
    ]
    entries = [{"message": {"content": [None, {"type": "thinking"}, {"type": "text", "text": "supported"}]}}]
    assert agent_trace._unsupported_io(records, entries) == 4


def test_collector_acceptance_is_logical_and_updates_are_distinct(export, transcript, monkeypatch):
    path, records = transcript
    outcomes = []
    monkeypatch.setattr(
        agent_trace,
        "_collector_outcomes",
        lambda session, spans, result, truncated: outcomes.append(
            (session, [s.span_id for s in spans], result, truncated)
        ),
    )
    flush(path)
    initial = set(export.observations)
    with path.open("a") as handle:
        handle.write(json.dumps(records[-1]) + "\n")
    flush(path)
    accepted = [i for session, ids, result, truncated in outcomes if result == "accepted" for i in ids]
    updated = [i for session, ids, result, truncated in outcomes if result == "updated" for i in ids]
    assert set(accepted) == initial and len(accepted) == len(initial)
    assert agent_trace._span_id("session", "tool") in updated
    assert all(session == "session" and truncated == 0 for session, ids, result, truncated in outcomes)


def test_field_compatibility_returns_masked_text_with_actual_cap(monkeypatch):
    monkeypatch.setattr("hooks.config.LANGFUSE_FIELD_MAX_CHARS", 3)
    assert agent_trace._field("abcdef") == "abc…[truncated 3 chars]"


def test_record_and_observation_revisions_are_canonical_for_unicode():
    import hashlib

    record = {"text": "ñ", "a": 1}
    expected = hashlib.sha256('{"a": 1, "text": "ñ"}'.encode()).hexdigest()
    assert agent_trace._record_revisions({"key": record}) == {"key": expected}
    assert agent_trace._record_revisions({"key": {"a": 1, "text": "ñ"}}) == {"key": expected}
    spec = agent_trace.SpanSpec("ñ", 1, None, 2, 3, {"z": 1, "a": "ñ"})
    reordered = agent_trace.SpanSpec("ñ", 1, None, 2, 3, {"a": "ñ", "z": 1})
    expected_span = hashlib.sha256(
        '{"attributes": {"a": "ñ", "z": 1}, "end_ns": 3, "name": "ñ", "parent_id": null, "span_id": 1, "start_ns": 2}'.encode()
    ).hexdigest()
    assert agent_trace._revision(spec) == agent_trace._revision(reordered) == expected_span


def test_strict_scalar_and_structured_secrets_remain_masked_when_mode_off(monkeypatch):
    from hooks.observability import transcript as source

    monkeypatch.setattr("hooks.config.SECRETS_MODE", "off")
    planted = "sk_" + "live_" + "Q7" * 18
    assert source.mask_value(planted) == "[REDACTED:stripe_key]"
    assert source.mask_value({"password": "controlled-literal-value"}) == {"password": "[REDACTED:generic_secret]"}


def test_legacy_source_alias_survives_full_replay(export, transcript):
    path, records = transcript
    if records[0]["type"] != "session_meta":
        pytest.skip("legacy aliases are Codex specific")
    cursor = agent_trace._cursor_path("session")
    cursor.parent.mkdir()
    cursor.write_text('{"turns":1}')
    flush(path)
    first = set(export.observations)
    state = agent_trace._cursor("session")
    ids = {key: record["_source_id"] for key, record in state["records"].items()}
    flush(path)
    assert {key: record["_source_id"] for key, record in agent_trace._cursor("session")["records"].items()} == ids
    assert set(export.observations) == first


def test_pending_size_exact_boundary_and_preparation_overflow(export, transcript, monkeypatch, capsys):
    path, _ = transcript
    state = agent_trace._progress("session")
    state["session_id"] = "session"
    agent_trace._stage_source(state, str(path))
    source_size = agent_trace._pending_bytes(state, state["records"], [])
    monkeypatch.setattr(agent_trace, "PENDING_MAX_BYTES", source_size)
    fresh = agent_trace._progress("session")
    fresh["session_id"] = "session"
    agent_trace._stage_source(fresh, str(path))
    assert "overflow" not in fresh
    agent_trace._prepare_pending("session", fresh, agent_trace.Identity("session"))
    assert fresh["overflow"]["bytes"] > fresh["overflow"]["limit"] == source_size
    assert capsys.readouterr().err == f"agent trace export overflow: {json.dumps(fresh['overflow'])}\n"
    monkeypatch.setattr(agent_trace, "PENDING_MAX_BYTES", 8_000_000)
    agent_trace._stage_source(fresh, str(path))
    assert "overflow" not in fresh
    agent_trace._prepare_pending("session", fresh, agent_trace.Identity("session"))
    limit = agent_trace._pending_bytes(fresh, fresh["records"], fresh["pending"])
    fresh["pending"] = []
    monkeypatch.setattr(agent_trace, "PENDING_MAX_BYTES", limit)
    agent_trace._prepare_pending("session", fresh, agent_trace.Identity("session"))
    assert "overflow" not in fresh and fresh["pending"]


def test_prepare_legacy_records_keeps_historical_acceptance(export, transcript):
    path, records = transcript
    state = agent_trace._progress("session")
    state["legacy_turns"] = 1
    agent_trace._stage_source(state, str(path))
    state.pop("unsupported_records", None)
    agent_trace._prepare_pending("session", state, agent_trace.Identity("session"))
    assert state["accepted"] and set(state["accepted"].values()) == {"legacy"}
    assert None not in state["accepted"]
    assert state["pending"][0]["attributes"]["agentihooks.export.unsupported_records"] == 0


@pytest.mark.parametrize("kind", ["text", "image", "tool_use", "tool_result"])
def test_all_supported_normalized_blocks_have_zero_unsupported_count(kind):
    assert agent_trace._unsupported_io([], [{"message": {"content": [{"type": kind}]}}]) == 0


def test_omitted_count_ignores_unrelated_source_messages():
    records = [{"type": "ignored", "payload": {"type": "message", "content": [{"type": "image"}, {"type": "audio"}]}}]
    assert agent_trace._unsupported_io(records, []) == 0


def test_http_encoded_payload_contains_the_observation(monkeypatch):
    import requests
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    exporter = OTLPSpanExporter(endpoint="http://localhost:1")
    response = requests.Response()
    response.status_code = 200
    response._content = b"{}"
    sent = []
    monkeypatch.setattr(otel, "langfuse_exporter_config", lambda: {"endpoint": "http://localhost:1", "headers": {}})
    monkeypatch.setattr(requests, "post", lambda endpoint, data, headers, timeout: sent.append(data) or response)
    assert agent_trace._batch_accepted(exporter, [agent_trace.SpanSpec("probe", 1, None, 2, 3)], 1)
    request = ExportTraceServiceRequest.FromString(sent[0])
    span = request.resource_spans[0].scope_spans[0].spans[0]
    assert span.name == "probe"
    assert span.span_id == (1).to_bytes(8, "big")
    assert span.trace_id == (1).to_bytes(16, "big")
    exporter.shutdown()


def test_unconfirmed_collector_outcome_carries_exact_truncation(export, transcript, monkeypatch):
    path, _ = transcript
    monkeypatch.setattr("hooks.config.LANGFUSE_FIELD_MAX_CHARS", 1)
    events = []
    monkeypatch.setattr(agent_trace, "_collector_outcomes", lambda *args: events.append(args))
    export.results = [SpanExportResult.FAILURE]
    flush(path)
    assert len(events) == 1
    session, spans, outcome, truncated = events[0]
    assert session == "session" and outcome == "unconfirmed"
    assert truncated == agent_trace._truncated_fields(spans) > 0


def test_empty_pending_state_remains_valid_and_has_no_accepted_data(export):
    state = agent_trace._progress("session")
    agent_trace._cursor_path("session").parent.mkdir()
    agent_trace._send_pending("session", state, export)
    assert state["accepted"] == {} and not state.get("accepted_records")
    assert state["source"] == {} and not state.get("turns")
    assert not export.calls


@pytest.mark.parametrize("key", ["password", "MY_API_KEY", "x password", "db password"])
@pytest.mark.parametrize(
    "value", ["controlled-literal-value", 12345678, ["controlled-literal-value"], {"value": 12345678}]
)
def test_contextual_masking_preserves_secret_field_structure(key, value, monkeypatch):
    from hooks.observability import transcript as source

    monkeypatch.setattr("hooks.config.SECRETS_MODE", "off")
    masked = source.mask_value({key: value})
    assert key in masked
    assert "controlled-literal-value" not in json.dumps(masked)
    assert "12345678" not in json.dumps(masked)
    if isinstance(value, list):
        assert masked[key] == ["[REDACTED:generic_secret]"]
    elif isinstance(value, dict):
        assert masked[key] == {"value": "[REDACTED:generic_secret]"}
    else:
        assert masked[key] == "[REDACTED:generic_secret]"


def test_contextual_masking_preserves_unrelated_types():
    from hooks.observability import transcript as source

    original = {
        "count": 12345678,
        "items": [True, None, {"name": "literal …[truncated 123 chars]"}],
        "password": "$ENV_REFERENCE",
    }
    assert source.mask_value(original) == original


def test_numeric_secret_is_masked_in_durable_tool_input(export, transcript):
    path, records = transcript
    if records[0]["type"] == "session_meta":
        records[-2]["payload"]["arguments"] = json.dumps({"password": 12345678})
    else:
        records[-2]["message"]["content"][0]["input"] = {"password": 12345678}
    path.write_text("".join(json.dumps(r) + "\n" for r in records[:-1]))
    export.results = [SpanExportResult.FAILURE]
    flush(path)
    cursor = agent_trace._cursor_path("session").read_text()
    assert "12345678" not in cursor
    assert "[REDACTED:generic_secret]" in cursor


def test_codex_native_call_identity_survives_rewritten_source():
    from hooks.observability import transcript as source

    first = {
        "type": "response_item",
        "timestamp": "before",
        "payload": {"type": "function_call", "call_id": "call", "arguments": '{"cmd":"before"}'},
    }
    replay = {
        "type": "response_item",
        "timestamp": "after",
        "payload": {"type": "function_call", "call_id": "call", "arguments": '{"cmd":"after"}'},
    }
    result = {
        "type": "response_item",
        "payload": {"type": "function_call_output", "call_id": "call", "output": "result"},
    }
    assert source.record_id(first) == source.record_id(replay)
    assert source.record_id(first) != source.record_id(result)


def test_repeated_tool_identity_keeps_one_logical_acceptance(export, transcript, monkeypatch):
    path, records = transcript
    outcomes = []
    monkeypatch.setattr(
        agent_trace,
        "_collector_outcomes",
        lambda session, spans, result, count: outcomes.append((result, [s.span_id for s in spans])),
    )
    duplicate = json.loads(json.dumps(records[-2]))
    if duplicate["type"] == "assistant":
        duplicate["uuid"] = "replayed-wrapper"
    else:
        duplicate["timestamp"] = "2026-10-07T10:00:01Z"
    with path.open("a") as handle:
        handle.write(json.dumps(duplicate) + "\n")
    flush(path)
    accepted = [span_id for result, ids in outcomes if result == "accepted" for span_id in ids]
    assert len(accepted) == len(set(accepted)) == len(export.observations)


def test_source_overflow_uses_exact_limit_and_event(export, transcript, monkeypatch):
    path, _ = transcript
    state = agent_trace._progress("session")
    state["session_id"] = "session"
    calls = []
    monkeypatch.setattr(
        agent_trace, "_report_progress", lambda progress, result: calls.append((dict(progress["overflow"]), result))
    )
    monkeypatch.setattr(agent_trace, "PENDING_MAX_BYTES", 1)
    agent_trace._stage_source(state, str(path))
    assert calls == [(state["overflow"], "overflow")]
    assert state["overflow"]["limit"] == 1 and state["overflow"]["bytes"] > 1
    assert state["records"] == {} and state["source"] == {}


def test_preparation_overflow_reports_combined_pending_payload(export, transcript, monkeypatch):
    path, _ = transcript
    state = agent_trace._progress("session")
    state["session_id"] = "session"
    agent_trace._stage_source(state, str(path))
    source_size = agent_trace._pending_bytes(state, state["records"], [])
    calls = []
    monkeypatch.setattr(
        agent_trace, "_report_progress", lambda progress, result: calls.append((dict(progress["overflow"]), result))
    )
    monkeypatch.setattr(agent_trace, "PENDING_MAX_BYTES", source_size + 1)
    agent_trace._prepare_pending("session", state, agent_trace.Identity("session"))
    assert state["pending"] == []
    assert calls == [(state["overflow"], "overflow")]
    assert state["overflow"]["limit"] == source_size + 1


@pytest.mark.parametrize("session,path", [("", "unused"), ("session", "")])
def test_missing_source_identity_exports_no_progress(export, session, path):
    agent_trace.export_session(session, path)
    assert not export.calls and not agent_trace.CURSOR_DIR.exists()


def test_native_generation_record_keys_preserve_kind_and_identifier():
    from hooks.observability import transcript as source

    record = {"type": "response_item", "payload": {"type": "message", "id": "generation", "content": ["before"]}}
    update = {"payload": {"type": "message", "id": "generation", "content": ["after"]}, "type": "response_item"}
    distinct = {"type": "response_item", "payload": {"type": "message", "id": "other", "content": ["before"]}}
    assert source.record_id(record) == source.record_id(update)
    assert source.record_id(record) != source.record_id(distinct)


def test_mask_embedded_json_and_preserve_plain_or_invalid_text():
    from hooks.observability import transcript as source

    assert source.mask_value('{"password":12345678}') == '{"password": "[REDACTED:generic_secret]"}'
    assert (
        source.mask_value(' [{"password":"controlled-literal-value"}]') == '[{"password": "[REDACTED:generic_secret]"}]'
    )
    assert source.mask_value('  {"literal": 12345678}  ') == '  {"literal": 12345678}  '
    assert source.mask_value("[invalid plain text") == "[invalid plain text"
    assert source.mask_value({"ordinary": {"password": 12345678}}) == {
        "ordinary": {"password": "[REDACTED:generic_secret]"}
    }


def test_transport_uses_declared_endpoint_headers_and_timeout(monkeypatch):
    import requests
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    settings = {"endpoint": "http://localhost:1", "headers": {"X-Context": "existing"}}
    response = requests.Response()
    response.status_code = 200
    response._content = b"{}"
    calls = []
    monkeypatch.setattr(otel, "langfuse_exporter_config", lambda: settings)

    def post(endpoint, data, headers, timeout):
        calls.append((endpoint, data, headers, timeout))
        return response

    monkeypatch.setattr(requests, "post", post)
    exporter = OTLPSpanExporter(endpoint=settings["endpoint"])
    assert agent_trace._batch_accepted(exporter, [agent_trace.SpanSpec("probe", 1, None, 2, 3)], 1)
    endpoint, data, headers, timeout = calls[0]
    assert endpoint == settings["endpoint"] and isinstance(data, bytes) and data
    assert requests.structures.CaseInsensitiveDict(headers)["Content-Type"].lower() == "application/x-protobuf"
    assert headers["X-Context"] == "existing" and timeout == otel.LANGFUSE_EXPORT_TIMEOUT_SEC
    exporter.shutdown()


def test_complete_metadata_waits_for_an_accepted_observation(export, tmp_path):
    path = tmp_path / "metadata.jsonl"
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": "session"}}) + "\n")
    flush(path)
    state = agent_trace._cursor("session")
    assert not export.calls and not state["accepted"]
    assert state["source"]["accepted_bytes"] == 0
    assert not state.get("accepted_records")
    assert state["records"]


@pytest.mark.parametrize(
    "acknowledgement",
    [
        None,
        [],
        {"unknown": True},
        {"error": "controlled"},
        {"errors": []},
        {"jobId": ""},
        {"id": "job"},
        {"id": "job", "name": "trace", "queueQualifiedName": "queue", "data": None},
    ],
)
def test_unknown_json_acknowledgement_is_retryable(acknowledgement):
    assert agent_trace._json_acknowledged(acknowledgement) is False


def test_deployed_queue_acknowledgement_is_accepted():
    receipt = {"id": "job", "name": "trace", "queueQualifiedName": "queue", "data": {}}
    assert agent_trace._json_acknowledged(receipt) is True
    for key in receipt:
        missing = {k: v for k, v in receipt.items() if k != key}
        assert agent_trace._json_acknowledged(missing) is False
    assert agent_trace._json_acknowledged({**receipt, "error": "controlled"}) is False


def test_legacy_source_bootstrap_carries_original_record_positions(export, transcript):
    path, records = transcript
    state = agent_trace._progress("session")
    state["legacy_turns"] = 1
    agent_trace._stage_source(state, str(path))
    for index, record in enumerate(records[:-1]):
        from hooks.observability.transcript import record_id

        saved = state["records"][record_id(record)]
        expected = str(index) if record["type"] == "response_item" else record_id(record)
        assert saved["_source_id"] == expected


def test_session_spans_ignore_unsupported_non_mapping_blocks():
    stamp = "2026-10-07T10:00:00Z"
    entries = [
        {"type": "user", "uuid": "prompt", "timestamp": stamp, "message": {"content": "probe"}},
        {
            "type": "assistant",
            "uuid": "response",
            "timestamp": stamp,
            "message": {"id": "generation", "content": [None]},
        },
    ]
    spans = agent_trace.session_spans(entries, agent_trace.Identity("session"))
    assert [s.attributes["langfuse.observation.type"] for s in spans] == ["agent", "span", "generation"]


def test_legacy_overflow_keeps_previous_acceptance_and_reports_only_overflow(export, transcript, monkeypatch, capsys):
    path, _ = transcript
    cursor = agent_trace._cursor_path("session")
    cursor.parent.mkdir()
    cursor.write_text('{"turns":1}')
    state = agent_trace._progress("session")
    agent_trace._stage_source(state, str(path))
    monkeypatch.setattr(agent_trace, "PENDING_MAX_BYTES", agent_trace._pending_bytes(state, state["records"], []) + 1)
    flush(path)
    result = agent_trace._cursor("session")
    assert result["overflow"] and result["accepted"] and not result["pending"]
    assert result["accepted_records"] == {} and result["source"]["accepted_bytes"] == 0
    assert result["turns"] == 0
    assert "agent_trace export failed" not in capsys.readouterr().err
    assert not export.calls


def test_invalid_json_text_uses_strict_redaction_with_operator_mode_off(monkeypatch):
    from hooks.observability import transcript as source

    monkeypatch.setattr("hooks.config.SECRETS_MODE", "off")
    planted = "sk_" + "live_" + "Q7" * 18
    assert source.mask_value("[invalid " + planted) == "[invalid [REDACTED:stripe_key]"


def test_masked_json_retains_unicode_characters():
    from hooks.observability import transcript as source

    assert (
        source.mask_value('{"password":12345678,"label":"ñ"}')
        == '{"password": "[REDACTED:generic_secret]", "label": "ñ"}'
    )


def test_nested_mapping_keys_are_strictly_masked_and_keep_child_context(monkeypatch):
    from hooks.observability import transcript as source

    monkeypatch.setattr("hooks.config.SECRETS_MODE", "off")
    planted = "sk_" + "live_" + "Q7" * 18
    assert source.mask_value({"ordinary": {planted: "literal"}}) == {"ordinary": {"[REDACTED:stripe_key]": "literal"}}
    assert source.mask_value({planted: {"password": 12345678}}) == {
        "[REDACTED:stripe_key]": {"password": "[REDACTED:generic_secret]"}
    }
    assert source.mask_value({"[REDACTED:generic_secret]": {"password": 12345678}}) == {
        "[REDACTED:generic_secret]": {"password": "[REDACTED:generic_secret]"}
    }


def test_short_unicode_literal_obeys_existing_secret_policy():
    from hooks.observability import transcript as source

    assert source.mask_value({"password": "ññ"}) == {"password": "ññ"}


@pytest.mark.parametrize(
    "acknowledgement",
    [
        {"jobId": 1},
        {"jobId": True},
        {"partialSuccess": None},
        {"partialSuccess": {"rejectedSpans": 0.5}},
        {"partialSuccess": {"rejectedSpans": False}},
        {"partialSuccess": {"rejectedSpans": "unknown"}},
        {"partialSuccess": {"rejectedSpans": -1}},
        {"partialSuccess": {"rejectedSpans": "00"}},
        {"id": 1, "name": "trace", "queueQualifiedName": "queue", "data": {}},
    ],
)
def test_malformed_typed_acknowledgements_are_retryable(acknowledgement):
    assert agent_trace._json_acknowledged(acknowledgement) is False


def test_codex_native_input_text_tool_result_is_exported(export, transcript):
    path, records = transcript
    if records[0]["type"] != "session_meta":
        pytest.skip("native input text tool results are Codex specific")
    records[-1]["payload"]["output"] = [{"type": "input_text", "text": "native output"}]
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    flush(path)
    tool = export.observations[agent_trace._span_id("session", "tool")]
    assert tool.attributes["langfuse.observation.output"] == "native output"


@pytest.mark.parametrize(
    "acknowledgement",
    [
        {"partial_success": {}},
        {"partialSuccess": {}},
        {"partial_success": {"rejected_spans": 0}},
        {"partial_success": {"rejected_spans": "0"}},
    ],
)
def test_zero_and_default_partial_acknowledgements_are_accepted(acknowledgement):
    assert agent_trace._json_acknowledged(acknowledgement) is True


def test_error_list_overrides_an_otherwise_valid_receipt():
    receipt = {"id": "job", "name": "trace", "queueQualifiedName": "queue", "data": {}, "errors": []}
    assert agent_trace._json_acknowledged(receipt) is False


def test_native_record_identity_uses_versioned_canonical_field_names():
    import hashlib

    from hooks.observability import transcript as source

    record = {"type": "response_item", "payload": {"type": "function_call", "id": "native"}}
    expected = hashlib.sha256('{"id":"native","kind":"function_call","type":"response_item"}'.encode()).hexdigest()
    assert source.record_id(record) == expected
    assert source.record_id({"type": "response_item"}) == hashlib.sha256(b'{"type":"response_item"}').hexdigest()


def test_preparation_reports_the_exact_pending_byte_count(export, transcript, monkeypatch):
    path, _ = transcript
    state = agent_trace._progress("session")
    agent_trace._stage_source(state, str(path))
    agent_trace._prepare_pending("session", state, agent_trace.Identity("session"))
    expected = len(json.dumps({"records": state["records"], "pending": state["pending"]}, ensure_ascii=False).encode())
    staged = agent_trace._progress("session")
    agent_trace._stage_source(staged, str(path))
    monkeypatch.setattr(agent_trace, "PENDING_MAX_BYTES", expected - 1)
    agent_trace._prepare_pending("session", staged, agent_trace.Identity("session"))
    assert staged["overflow"] == {"bytes": expected, "limit": expected - 1}


@pytest.mark.parametrize("kind", ["function_call_output", "custom_tool_call_output"])
@pytest.mark.parametrize("block_type", ["input_text", "output_text", "text"])
def test_codex_native_result_text_blocks_keep_their_content(kind, block_type):
    from hooks.observability import codex_transcript

    assert codex_transcript._content(
        {"type": kind, "call_id": "call", "output": [{"type": block_type, "text": "output"}]}
    ) == [{"type": "tool_result", "tool_use_id": "call", "content": [{"type": "text", "text": "output"}]}]


def test_codex_native_result_keeps_other_block_types_for_reporting():
    from hooks.observability import codex_transcript

    blocks = [None, {"type": "audio", "data": "unsupported"}]
    assert (
        codex_transcript._content({"type": "function_call_output", "call_id": "call", "output": blocks})[0]["content"]
        == blocks
    )


def test_legacy_source_without_header_keeps_original_position(export, tmp_path):
    from hooks.observability.transcript import record_id

    record = {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"text": "probe"}]}}
    path = tmp_path / "source.jsonl"
    path.write_text(json.dumps(record) + "\n")
    state = agent_trace._progress("session")
    state["legacy_turns"] = 1
    agent_trace._stage_source(state, str(path))
    assert state["records"][record_id(record)]["_source_id"] == "0"


def test_codex_empty_native_result_keeps_declared_field_types():
    from hooks.observability import codex_transcript

    assert codex_transcript._content({"type": "function_call_output"}) == [
        {"type": "tool_result", "tool_use_id": "", "content": ""}
    ]


def test_literal_marker_and_secret_in_key_do_not_mask_public_values():
    from hooks.observability import transcript as source

    planted = "sk_" + "live_" + "Q7" * 18
    key = "[REDACTED:generic_secret] " + planted
    expected_key = "[REDACTED:generic_secret] [REDACTED:stripe_key]"
    assert source.mask_value({key: "controlled-literal-value"}) == {expected_key: "controlled-literal-value"}
    assert source.mask_value({key: {"value": "controlled-literal-value"}}) == {
        expected_key: {"value": "controlled-literal-value"}
    }
    assert source.mask_value({key: {"password": 12345678}}) == {expected_key: {"password": "[REDACTED:generic_secret]"}}


def test_existing_marker_in_sensitive_value_cannot_hide_literal_suffix():
    from hooks.observability import transcript as source
    from hooks.secrets import contains_generic_secret

    value = "[REDACTED:generic_secret]controlled-literal-value"
    assert contains_generic_secret('password="' + value + '"') is True
    assert source.mask_value({"password": value}) == {"password": "[REDACTED:generic_secret]"}
    assert contains_generic_secret('ordinary="' + value + '"') is False
    assert source.mask_value({"ordinary": value}) == {"ordinary": value}
    assert contains_generic_secret("password=$ENV_REFERENCE") is False
