import json
from unittest.mock import patch

from hooks.observability import agent_trace

RECORDS = [
    {"type": "session_meta", "payload": {"id": "codex-session"}},
    {"type": "turn_context", "payload": {"model": "gpt-test"}},
    {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "developer",
            "content": [{"type": "input_text", "text": "instructions"}],
        },
    },
    {
        "type": "response_item",
        "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Read the file"}]},
    },
    {"type": "event_msg", "payload": {"type": "user_message", "message": "Read the file"}},
    {
        "type": "response_item",
        "payload": {"type": "function_call", "name": "exec_command", "call_id": "call-1", "arguments": '{"cmd":"pwd"}'},
    },
    {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "call-1", "output": "/workspace"}},
    {
        "type": "response_item",
        "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Done"}]},
    },
    {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "last_token_usage": {
                    "input_tokens": 30,
                    "cached_input_tokens": 10,
                    "output_tokens": 5,
                    "total_tokens": 35,
                },
                "total_token_usage": {"total_tokens": 35},
            },
        },
    },
    {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "Done"}},
]


def _rollout(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.write_text(
        "\n".join(
            json.dumps({"timestamp": f"2026-10-05T10:00:{index:02d}Z", **entry}) for index, entry in enumerate(RECORDS)
        )
    )
    return str(path)


def test_codex_native_records_reach_shared_span_builder(tmp_path):
    entries = agent_trace.read_entries(_rollout(tmp_path))
    assert len(agent_trace.turns(entries)) == 1
    assert entries[0]["message"]["content"] == [{"type": "text", "text": "Read the file"}]
    assert entries[1]["message"]["content"][0] == {
        "type": "tool_use",
        "id": "call-1",
        "name": "exec_command",
        "input": {"cmd": "pwd"},
    }
    assert entries[2]["message"]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "call-1",
        "content": "/workspace",
    }
    identity = agent_trace.Identity("codex-session", "manual", "", "", "", "")
    spans = agent_trace.session_spans(entries, identity)
    tools = [span for span in spans if span.attributes.get("langfuse.observation.type") == "tool"]
    assert len(tools) == 1
    assert tools[0].end_ns > tools[0].start_ns
    generations = [span for span in spans if span.attributes.get("gen_ai.response.model") == "gpt-test"]
    assert sum(span.attributes["gen_ai.usage.input_tokens"] for span in generations) == 20
    assert sum(span.attributes["gen_ai.usage.cache_read_input_tokens"] for span in generations) == 10
    assert sum(span.attributes["gen_ai.usage.output_tokens"] for span in generations) == 5


def test_codex_export_uses_existing_exporter_and_cursor(tmp_path, monkeypatch):
    from opentelemetry.sdk.trace.export import SpanExportResult

    monkeypatch.setattr(agent_trace, "CURSOR_DIR", tmp_path / "cursors")
    exporter = type(
        "Exporter", (), {"export": lambda self, spans: SpanExportResult.SUCCESS, "shutdown": lambda self: None}
    )()
    with (
        patch("hooks.observability.otel.langfuse_exporter", return_value=exporter),
        patch("hooks.context.context_usage.session_cost", return_value=0),
    ):
        agent_trace.export_session("codex-session", _rollout(tmp_path))
    assert json.loads((tmp_path / "cursors" / "codex-session.json").read_text())["turns"] == 1


def test_custom_tools_and_repeated_usage_keep_one_call_and_one_charge():
    from hooks.observability.codex_transcript import normalize_entries

    records = [
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"text": "Run"}]}},
        {
            "type": "response_item",
            "payload": {"type": "custom_tool_call", "name": "functions.exec", "call_id": "custom", "input": "text(1)"},
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"last_token_usage": {"input_tokens": 9}, "total_token_usage": {"total_tokens": 9}},
            },
        },
        {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "custom", "output": "1"}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"text": "Done"}]}},
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"last_token_usage": {"input_tokens": 9}, "total_token_usage": {"total_tokens": 9}},
            },
        },
    ]
    entries = normalize_entries(records)
    assert entries[1]["message"]["content"][0]["input"] == "text(1)"
    assert entries[2]["message"]["content"][0]["tool_use_id"] == "custom"
    assert sum(entry["message"].get("usage", {}).get("input_tokens", 0) for entry in entries) == 9
