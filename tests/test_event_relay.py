"""Tests for hooks.observability.event_relay."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def fake_redis(monkeypatch):
    import fakeredis
    fake = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(
        "hooks.observability.event_relay._get_redis",
        lambda: fake,
    )
    return fake


class TestExtractAssistant:
    def test_extracts_thinking(self):
        from hooks.observability.event_relay import extract_events_from_assistant
        entry = {
            "type": "assistant",
            "message": {"content": [
                {"type": "thinking", "thinking": "I should think hard."},
            ]},
        }
        events = extract_events_from_assistant(entry)
        assert len(events) == 1
        assert events[0]["event_type"] == "thinking"
        assert events[0]["content"] == "I should think hard."

    def test_extracts_text(self):
        from hooks.observability.event_relay import extract_events_from_assistant
        entry = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Hello world"}]},
        }
        events = extract_events_from_assistant(entry)
        assert events == [{"event_type": "assistant_text", "content": "Hello world"}]

    def test_extracts_tool_use(self):
        from hooks.observability.event_relay import extract_events_from_assistant
        entry = {
            "type": "assistant",
            "message": {"content": [
                {"type": "tool_use", "id": "tu_1", "name": "Bash", "input": {"command": "ls"}},
            ]},
        }
        events = extract_events_from_assistant(entry)
        assert len(events) == 1
        assert events[0]["event_type"] == "tool_use"
        parsed = json.loads(events[0]["content"])
        assert parsed == {"id": "tu_1", "name": "Bash", "input": {"command": "ls"}}

    def test_extracts_mixed_blocks(self):
        from hooks.observability.event_relay import extract_events_from_assistant
        entry = {
            "type": "assistant",
            "message": {"content": [
                {"type": "thinking", "thinking": "thinking..."},
                {"type": "text", "text": "Working on it."},
                {"type": "tool_use", "id": "tu_2", "name": "Read", "input": {}},
            ]},
        }
        events = extract_events_from_assistant(entry)
        assert [e["event_type"] for e in events] == ["thinking", "assistant_text", "tool_use"]

    def test_skips_empty_thinking_and_text(self):
        from hooks.observability.event_relay import extract_events_from_assistant
        entry = {
            "type": "assistant",
            "message": {"content": [
                {"type": "thinking", "thinking": ""},
                {"type": "text", "text": ""},
            ]},
        }
        assert extract_events_from_assistant(entry) == []

    def test_unknown_block_type_ignored(self):
        from hooks.observability.event_relay import extract_events_from_assistant
        entry = {
            "type": "assistant",
            "message": {"content": [{"type": "unknown_thing", "data": "x"}]},
        }
        assert extract_events_from_assistant(entry) == []

    def test_non_list_content(self):
        from hooks.observability.event_relay import extract_events_from_assistant
        entry = {"type": "assistant", "message": {"content": "string content"}}
        assert extract_events_from_assistant(entry) == []


class TestExtractUser:
    def test_extracts_tool_result_string(self):
        from hooks.observability.event_relay import extract_events_from_user
        entry = {
            "type": "user",
            "message": {"content": [
                {"type": "tool_result", "tool_use_id": "tu_1", "content": "ok"},
            ]},
        }
        events = extract_events_from_user(entry)
        assert len(events) == 1
        assert events[0]["event_type"] == "tool_result"
        parsed = json.loads(events[0]["content"])
        assert parsed["tool_use_id"] == "tu_1"
        assert parsed["output"] == "ok"
        assert parsed["is_error"] is False

    def test_extracts_tool_result_list_text(self):
        from hooks.observability.event_relay import extract_events_from_user
        entry = {
            "type": "user",
            "message": {"content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_2",
                    "content": [
                        {"type": "text", "text": "line1"},
                        {"type": "text", "text": "line2"},
                    ],
                },
            ]},
        }
        events = extract_events_from_user(entry)
        parsed = json.loads(events[0]["content"])
        assert parsed["output"] == "line1\nline2"

    def test_propagates_is_error(self):
        from hooks.observability.event_relay import extract_events_from_user
        entry = {
            "type": "user",
            "message": {"content": [
                {"type": "tool_result", "tool_use_id": "x", "content": "boom", "is_error": True},
            ]},
        }
        events = extract_events_from_user(entry)
        parsed = json.loads(events[0]["content"])
        assert parsed["is_error"] is True

    def test_skips_non_tool_result_blocks(self):
        from hooks.observability.event_relay import extract_events_from_user
        entry = {
            "type": "user",
            "message": {"content": [{"type": "text", "text": "just text"}]},
        }
        assert extract_events_from_user(entry) == []

    def test_string_content_ignored(self):
        from hooks.observability.event_relay import extract_events_from_user
        entry = {"type": "user", "message": {"content": "plain string"}}
        assert extract_events_from_user(entry) == []


class TestExtractDispatcher:
    def test_dispatches_assistant(self):
        from hooks.observability.event_relay import extract_events_from_entry
        entry = {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}
        events = extract_events_from_entry(entry)
        assert events[0]["event_type"] == "assistant_text"

    def test_dispatches_user(self):
        from hooks.observability.event_relay import extract_events_from_entry
        entry = {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "x", "content": "y"},
        ]}}
        events = extract_events_from_entry(entry)
        assert events[0]["event_type"] == "tool_result"

    def test_other_types_return_empty(self):
        from hooks.observability.event_relay import extract_events_from_entry
        for t in ("system", "file-history-snapshot", "queue-operation"):
            assert extract_events_from_entry({"type": t}) == []


class TestPositionTracking:
    def test_load_default_zero(self, fake_redis):
        from hooks.observability.event_relay import _load_position
        assert _load_position("session-new") == 0

    def test_save_and_load(self, fake_redis):
        from hooks.observability.event_relay import _load_position, _save_position
        _save_position("session-1", 1234)
        assert _load_position("session-1") == 1234

    def test_file_fallback(self, monkeypatch, tmp_path):
        import hooks.observability.event_relay as mod
        monkeypatch.setattr(mod, "_get_redis", lambda: None)
        monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
        mod._save_position("session-x", 999)
        assert mod._load_position("session-x") == 999


class TestReadNewTranscriptEvents:
    def test_reads_new_lines_only(self, fake_redis, tmp_path):
        from hooks.observability.event_relay import read_new_transcript_events
        path = tmp_path / "transcript.jsonl"
        path.write_text(
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "first"}]}}) + "\n"
        )
        events_1, pos_1 = read_new_transcript_events("sess-1", str(path))
        assert len(events_1) == 1
        assert pos_1 > 0

        from hooks.observability.event_relay import _save_position
        _save_position("sess-1", pos_1)

        with open(path, "a") as f:
            f.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "second"}]}}) + "\n")

        events_2, pos_2 = read_new_transcript_events("sess-1", str(path))
        assert len(events_2) == 1
        assert events_2[0]["content"] == "second"
        assert pos_2 > pos_1

    def test_missing_file_returns_empty(self, fake_redis):
        from hooks.observability.event_relay import read_new_transcript_events
        events, pos = read_new_transcript_events("sess-x", "/nonexistent/path.jsonl")
        assert events == []
        assert pos == 0

    def test_malformed_lines_skipped(self, fake_redis, tmp_path):
        from hooks.observability.event_relay import read_new_transcript_events
        path = tmp_path / "t.jsonl"
        path.write_text(
            "not json\n"
            + json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}}) + "\n"
            + "{broken\n"
        )
        events, _ = read_new_transcript_events("s", str(path))
        assert len(events) == 1
        assert events[0]["content"] == "ok"


class TestPublish:
    def test_publish_events_xadd(self, fake_redis):
        from hooks.observability.event_relay import publish_events, _stream_key
        events = [
            {"event_type": "thinking", "content": "thinking text"},
            {"event_type": "assistant_text", "content": "hello"},
        ]
        n = publish_events("corr-1", "sess-1", events)
        assert n == 2
        key = _stream_key("corr-1")
        items = fake_redis.xrange(key)
        assert len(items) == 2
        assert items[0][1]["event_type"] == "thinking"
        assert items[0][1]["content"] == "thinking text"
        assert items[1][1]["event_type"] == "assistant_text"

    def test_publish_done_sentinel(self, fake_redis):
        from hooks.observability.event_relay import publish_done, _stream_key
        publish_done("corr-2", "sess-2")
        items = fake_redis.xrange(_stream_key("corr-2"))
        assert len(items) == 1
        assert items[0][1]["event_type"] == "done"

    def test_publish_empty_no_op(self, fake_redis):
        from hooks.observability.event_relay import publish_events, _stream_key
        n = publish_events("corr-3", "sess-3", [])
        assert n == 0
        assert fake_redis.exists(_stream_key("corr-3")) == 0

    def test_publish_no_redis_returns_zero(self, monkeypatch):
        import hooks.observability.event_relay as mod
        monkeypatch.setattr(mod, "_get_redis", lambda: None)
        n = mod.publish_events("c", "s", [{"event_type": "thinking", "content": "x"}])
        assert n == 0


class TestEventsFromPostToolUse:
    def test_real_claude_code_payload_shape(self):
        """Mirror Claude Code's actual PostToolUse hook event."""
        from hooks.observability.event_relay import events_from_post_tool_use
        payload = {
            "hook_event_name": "PostToolUse",
            "session_id": "abc",
            "tool_name": "Bash",
            "tool_input": {"command": "echo hi", "description": "Echo"},
            "tool_use_id": "toolu_xyz",
            "tool_response": {"stdout": "hi\n", "stderr": "", "is_error": False},
        }
        events = events_from_post_tool_use(payload)
        assert len(events) == 2
        assert events[0]["event_type"] == "tool_use"
        tu = json.loads(events[0]["content"])
        assert tu["name"] == "Bash"
        assert tu["input"] == {"command": "echo hi", "description": "Echo"}
        assert tu["id"] == "toolu_xyz"
        assert events[1]["event_type"] == "tool_result"
        tr = json.loads(events[1]["content"])
        assert tr["output"] == "hi\n"
        assert tr["is_error"] is False
        assert tr["tool_use_id"] == "toolu_xyz"

    def test_legacy_tool_output_field_still_works(self):
        from hooks.observability.event_relay import events_from_post_tool_use
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "tool_output": "file1\nfile2",
            "is_error": False,
            "tool_use_id": "tu_42",
        }
        events = events_from_post_tool_use(payload)
        assert len(events) == 2
        tr = json.loads(events[1]["content"])
        assert tr["output"] == "file1\nfile2"

    def test_skips_system_tools(self):
        from hooks.observability.event_relay import events_from_post_tool_use
        payload = {"tool_name": "system/internal", "tool_input": {}, "tool_response": "x"}
        assert events_from_post_tool_use(payload) == []

    def test_missing_tool_name_skipped(self):
        from hooks.observability.event_relay import events_from_post_tool_use
        assert events_from_post_tool_use({}) == []

    def test_dict_response_falls_through_to_json(self):
        from hooks.observability.event_relay import events_from_post_tool_use
        payload = {
            "tool_name": "MyTool",
            "tool_input": {},
            "tool_response": {"foo": "bar"},
        }
        events = events_from_post_tool_use(payload)
        assert len(events) == 2
        tr = json.loads(events[1]["content"])
        assert json.loads(tr["output"]) == {"foo": "bar"}

    def test_is_error_in_response(self):
        from hooks.observability.event_relay import events_from_post_tool_use
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "false"},
            "tool_response": {"stdout": "", "stderr": "boom", "is_error": True},
        }
        events = events_from_post_tool_use(payload)
        tr = json.loads(events[1]["content"])
        assert tr["is_error"] is True

    def test_is_error_top_level_only(self):
        from hooks.observability.event_relay import events_from_post_tool_use
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "x"},
            "tool_response": "boom",
            "is_error": True,
        }
        events = events_from_post_tool_use(payload)
        tr = json.loads(events[1]["content"])
        assert tr["is_error"] is True

    def test_no_response_no_result(self):
        from hooks.observability.event_relay import events_from_post_tool_use
        payload = {"tool_name": "Bash", "tool_input": {}, "tool_use_id": "x"}
        events = events_from_post_tool_use(payload)
        assert len(events) == 1
        assert events[0]["event_type"] == "tool_use"

    def test_is_error_none_treated_as_false(self):
        """Real-world Claude Code: is_error may be None (not set)."""
        from hooks.observability.event_relay import events_from_post_tool_use
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "tool_use_id": "x",
            "tool_response": {"stdout": "data\n"},
            "is_error": None,
        }
        events = events_from_post_tool_use(payload)
        tr = json.loads(events[1]["content"])
        assert tr["is_error"] is False
        assert tr["output"] == "data\n"


class TestEventsFromStopPayload:
    def test_extracts_last_assistant_message(self):
        from hooks.observability.event_relay import events_from_stop_payload
        payload = {"last_assistant_message": "Hello, world!"}
        events = events_from_stop_payload(payload)
        assert events == [{"event_type": "assistant_text", "content": "Hello, world!"}]

    def test_empty_message_skipped(self):
        from hooks.observability.event_relay import events_from_stop_payload
        assert events_from_stop_payload({"last_assistant_message": ""}) == []
        assert events_from_stop_payload({}) == []
        assert events_from_stop_payload({"last_assistant_message": "   "}) == []

    def test_non_string_skipped(self):
        from hooks.observability.event_relay import events_from_stop_payload
        assert events_from_stop_payload({"last_assistant_message": ["x", "y"]}) == []
