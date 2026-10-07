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
    monkeypatch.setattr(exporter, "_export", lambda payload, timeout: sent.append((payload, timeout)) or response)
    spec = agent_trace.SpanSpec("probe", 1, None, 1, 2, {"value": 3})
    if accepted is None:
        with pytest.raises(ValueError):
            agent_trace._batch_accepted(exporter, [spec], 1)
    else:
        assert agent_trace._batch_accepted(exporter, [spec], 1) is accepted
    assert len(sent) == 1 and sent[0][1] == exporter._timeout
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
    monkeypatch.setattr(exporter, "_export", lambda *args: response)
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
