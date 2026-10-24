"""SV2-SES-01: per-entry record normalization shared by the whole-file reader and incremental consumers."""

import json
from pathlib import Path

import pytest

from hooks.memory.records import CODEX_TYPES, DECODERS, TurnState, is_copilot_type, normalize_entries
from hooks.memory.transcript_reader import _load_entries, detect_transcript_format, iter_transcript_records

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLES = {
    "claude": FIXTURES / "claude_transcript_sample.jsonl",
    "codex": FIXTURES / "codex_rollout_sample.jsonl",
    "copilot": FIXTURES / "copilot_events_sample.jsonl",
}

CLAUDE_INJECTED = [
    {"type": "user", "message": {"content": "fix the bug"}},
    {
        "type": "user",
        "isMeta": True,
        "message": {"content": [{"type": "text", "text": "<system-reminder>ignore</system-reminder>"}]},
    },
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "Fixed."}]}},
]
CODEX_TURN = [
    {"type": "session_meta", "payload": {"id": "s2"}},
    {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "developer",
            "content": [{"type": "input_text", "text": "=== CONTEXT INJECTION ==="}],
        },
    },
    {
        "type": "response_item",
        "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]},
    },
    {
        "type": "response_item",
        "payload": {"type": "function_call", "name": "shell", "arguments": "{}", "call_id": "c9"},
    },
    {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c9", "output": "ok"}},
    {
        "type": "response_item",
        "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Done."}]},
    },
    {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "Done."}},
]
COPILOT_INJECTED = [
    {"type": "user.message", "data": {"content": "list files"}},
    {"type": "assistant.message", "data": {"content": "Listed."}},
    {"type": "user.message", "data": {"content": "=== CONTEXT INJECTION ===", "source": "system"}},
    {"type": "session.task_complete", "data": {"summary": "Listed."}},
]


def _write(tmp_path: Path, name: str, entries: list[dict]) -> Path:
    path = tmp_path / name
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))
    return path


@pytest.mark.parametrize("fmt", sorted(SAMPLES))
def test_stream_matches_whole_file_reader(fmt):
    path = SAMPLES[fmt]
    assert detect_transcript_format(path) == fmt
    state = TurnState()
    streamed = [rec for entry in _load_entries(path) for rec in normalize_entries(fmt, [entry], state)]
    assert streamed == list(iter_transcript_records(path))
    assert streamed


def test_second_independent_fixture_matches(tmp_path):
    claude = FIXTURES / "claude_injected_sample.jsonl"
    recs = list(normalize_entries("claude", _load_entries(claude)))
    assert recs == list(iter_transcript_records(claude))
    assert [r["kind"] for r in recs] == [
        "user_text",
        "system_text",
        "assistant_text",
        "tool_call",
        "tool_result",
        "assistant_text",
    ]
    path = _write(tmp_path, "rollout.jsonl", CODEX_TURN)
    assert list(normalize_entries("codex", CODEX_TURN)) == list(iter_transcript_records(path))
    real = FIXTURES / "copilot_events_real.jsonl"
    assert list(normalize_entries("copilot", _load_entries(real))) == list(iter_transcript_records(real))


def test_decoders_cover_every_provider():
    assert set(DECODERS) == {"claude", "codex", "copilot"}


def test_unknown_format_is_refused():
    with pytest.raises(KeyError):
        list(normalize_entries("gemini", CODEX_TURN))


def test_non_mapping_entries_are_skipped():
    assert list(normalize_entries("claude", ["x", 3, None])) == []


@pytest.mark.parametrize(
    ("fmt", "entries", "injected"),
    [
        ("claude", CLAUDE_INJECTED, "<system-reminder>ignore</system-reminder>"),
        ("codex", CODEX_TURN, "=== CONTEXT INJECTION ==="),
        ("copilot", COPILOT_INJECTED, "=== CONTEXT INJECTION ==="),
    ],
)
def test_injected_context_never_becomes_user_text(fmt, entries, injected):
    snapshot = json.dumps(entries)
    recs = list(normalize_entries(fmt, entries))
    assert [r["text"] for r in recs if r["kind"] == "system_text"] == [injected]
    assert injected not in [r.get("text") for r in recs if r["kind"] == "user_text"]
    assert len([r for r in recs if r["kind"] == "user_text"]) == 1
    assert json.dumps(entries) == snapshot


def test_injected_context_does_not_open_a_turn():
    recs = list(normalize_entries("copilot", COPILOT_INJECTED))
    assert [r["kind"] for r in recs if r["kind"] == "turn_complete"] == []


@pytest.mark.parametrize("fmt", ["codex", "copilot"])
def test_restart_from_saved_state_counts_completion_once(fmt):
    entries = CODEX_TURN if fmt == "codex" else COPILOT_INJECTED
    cut = len(entries) - 1
    first = TurnState()
    head = list(normalize_entries(fmt, entries[:cut], first))
    saved = json.loads(json.dumps(first.to_dict()))
    assert saved == {"assistant_seen": True}
    tail = list(normalize_entries(fmt, entries[cut:], TurnState.from_dict(saved)))
    assert head + tail == list(normalize_entries(fmt, entries))
    assert [r for r in tail if r["kind"] == "turn_complete"] == []


def test_restart_without_saved_state_would_recount():
    tail = list(normalize_entries("codex", CODEX_TURN[-1:], TurnState()))
    assert [r["kind"] for r in tail] == ["turn_complete"]


def test_completion_resets_state_for_the_next_turn():
    state = TurnState(assistant_seen=True)
    list(normalize_entries("codex", CODEX_TURN[-1:], state))
    assert state.to_dict() == {"assistant_seen": False}


def test_from_dict_defaults_when_state_is_missing():
    assert TurnState.from_dict({}) == TurnState(assistant_seen=False)
    assert TurnState.from_dict({"assistant_seen": 1}) == TurnState(assistant_seen=True)


def test_inbox_channel_delivery_stays_a_prompt():
    entry = {
        "type": "user",
        "isMeta": True,
        "origin": {"kind": "channel", "server": "inbox"},
        "message": {"content": "review the branch"},
    }
    assert [r["kind"] for r in normalize_entries("claude", [entry])] == ["user_text"]
    other = dict(entry, origin={"kind": "channel", "server": "brain"})
    assert [r["kind"] for r in normalize_entries("claude", [other])] == ["system_text"]


def test_codex_rollout_golden_records():
    recs = list(iter_transcript_records(SAMPLES["codex"]))
    assert [(r["kind"], r.get("text", r.get("tool_use_id"))) for r in recs] == [
        ("meta", None),
        ("system_text", "=== CONTEXT INJECTION ===\ninjected banner"),
        ("user_text", "fix the bug"),
        ("tool_call", "c1"),
        ("tool_result", "c1"),
        ("assistant_text", "Done.\n<!-- @lesson -->\ncodex rollouts use response_item records\n<!-- @/lesson -->"),
        ("token_usage", None),
    ]


_CLAUDE_BLOCKS = [
    "x",
    {"type": "text"},
    {"type": "tool_use"},
    {"type": "tool_result"},
    {"type": "image"},
    {"type": "text", "text": "t"},
    {"type": "tool_use", "id": "u", "name": "N", "input": {"a": 1}},
    {"type": "tool_result", "tool_use_id": "u", "content": "c", "is_error": "yes"},
]
_CODEX_TEXTS = [
    {"type": "input_text", "text": "a"},
    {"type": "output_text", "text": "b"},
    {"type": "text", "text": "c"},
    {"type": "image", "text": "no"},
    "s",
    {"type": "input_text"},
]

DECODE_CASES = [
    ("claude", {"type": "system", "message": "m"}, False, [], False),
    ("claude", {"type": "user", "message": "hi"}, True, [{"kind": "user_text", "text": "hi"}], True),
    (
        "claude",
        {"type": "assistant", "message": {"content": "ok"}},
        False,
        [{"kind": "assistant_text", "text": "ok"}],
        False,
    ),
    ("claude", {"type": "user", "isMeta": True, "message": "r"}, False, [{"kind": "system_text", "text": "r"}], False),
    (
        "claude",
        {"type": "assistant", "isMeta": True, "message": "a"},
        False,
        [{"kind": "assistant_text", "text": "a"}],
        False,
    ),
    ("claude", {"type": "assistant", "message": {"content": 5}}, False, [], False),
    ("claude", {"type": "assistant"}, False, [], False),
    ("claude", {"type": "assistant", "message": {}}, False, [], False),
    (
        "claude",
        {"type": "assistant", "message": {"content": _CLAUDE_BLOCKS}},
        False,
        [
            {"kind": "assistant_text", "text": ""},
            {"kind": "tool_call", "tool_name": "", "tool_input": None, "tool_use_id": ""},
            {"kind": "tool_result", "tool_result": None, "tool_use_id": "", "is_error": False},
            {"kind": "assistant_text", "text": "t"},
            {"kind": "tool_call", "tool_name": "N", "tool_input": {"a": 1}, "tool_use_id": "u"},
            {"kind": "tool_result", "tool_result": "c", "tool_use_id": "u", "is_error": True},
        ],
        False,
    ),
    ("codex", {"type": "response_item", "payload": "s"}, False, [], False),
    ("codex", {"type": "session_meta"}, False, [{"kind": "meta"}], False),
    ("codex", {"type": "turn_context", "payload": {}}, False, [], False),
    (
        "codex",
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": _CODEX_TEXTS}},
        True,
        [{"kind": "user_text", "text": "a\nb\nc\n"}],
        False,
    ),
    (
        "codex",
        {
            "type": "response_item",
            "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "r"}]},
        },
        False,
        [{"kind": "assistant_text", "text": "r"}],
        True,
    ),
    (
        "codex",
        {"type": "response_item", "payload": {"type": "message", "content": [{"type": "text", "text": "d"}]}},
        True,
        [{"kind": "system_text", "text": "d"}],
        True,
    ),
    ("codex", {"type": "response_item", "payload": {"type": "message", "role": "assistant"}}, False, [], False),
    (
        "codex",
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": []}},
        True,
        [],
        False,
    ),
    (
        "codex",
        {"type": "response_item", "payload": {"type": "function_call", "name": "n", "arguments": "{}", "call_id": "c"}},
        False,
        [{"kind": "tool_call", "tool_name": "n", "tool_input": "{}", "tool_use_id": "c"}],
        False,
    ),
    (
        "codex",
        {"type": "response_item", "payload": {"type": "local_shell_call", "action": {"x": 1}}},
        False,
        [{"kind": "tool_call", "tool_name": "local_shell_call", "tool_input": {"x": 1}, "tool_use_id": ""}],
        False,
    ),
    (
        "codex",
        {"type": "response_item", "payload": {"type": "custom_tool_call", "arguments": "", "action": "a"}},
        False,
        [{"kind": "tool_call", "tool_name": "custom_tool_call", "tool_input": "a", "tool_use_id": ""}],
        False,
    ),
    (
        "codex",
        {"type": "response_item", "payload": {"type": "function_call_output", "output": "o", "call_id": "c"}},
        False,
        [{"kind": "tool_result", "tool_result": "o", "tool_use_id": "c", "is_error": False}],
        False,
    ),
    (
        "codex",
        {"type": "response_item", "payload": {"type": "custom_tool_call_output"}},
        False,
        [{"kind": "tool_result", "tool_result": None, "tool_use_id": "", "is_error": False}],
        False,
    ),
    ("codex", {"type": "response_item", "payload": {"type": "reasoning"}}, True, [], True),
    ("codex", {"type": "event_msg", "payload": {"type": "token_count"}}, True, [{"kind": "token_usage"}], True),
    (
        "codex",
        {"type": "event_msg", "payload": {"type": "task_complete"}},
        False,
        [{"kind": "turn_complete", "text": ""}],
        False,
    ),
    (
        "codex",
        {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "m"}},
        False,
        [{"kind": "turn_complete", "text": "m"}],
        False,
    ),
    ("codex", {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "m"}}, True, [], False),
    ("codex", {"type": "event_msg", "payload": {"type": "task_started"}}, True, [], True),
    ("copilot", {"type": "user", "data": {"content": "u"}}, False, [], False),
    ("copilot", {"type": "user.message", "ephemeral": True, "data": {"content": "u"}}, True, [], True),
    ("copilot", {"type": "assistant.usage", "ephemeral": True, "data": {}}, False, [{"kind": "token_usage"}], False),
    ("copilot", {"type": "user.message", "data": "u"}, True, [], True),
    ("copilot", {"type": "user.message"}, True, [], True),
    (
        "copilot",
        {"type": "user.message", "data": {"content": "ctx", "source": "system"}},
        True,
        [{"kind": "system_text", "text": "ctx"}],
        True,
    ),
    ("copilot", {"type": "user.message", "data": {"content": "", "source": "system"}}, True, [], True),
    (
        "copilot",
        {"type": "user.message", "data": {"content": "u", "source": "user"}},
        True,
        [{"kind": "user_text", "text": "u"}],
        False,
    ),
    ("copilot", {"type": "user.message", "data": {"content": ""}}, True, [], False),
    ("copilot", {"type": "user.message", "data": {}}, True, [], False),
    (
        "copilot",
        {"type": "assistant.message", "data": {"content": "a"}},
        False,
        [{"kind": "assistant_text", "text": "a"}],
        True,
    ),
    ("copilot", {"type": "assistant.message", "data": {}}, False, [], False),
    (
        "copilot",
        {"type": "system.message", "data": {"content": "s"}},
        True,
        [{"kind": "system_text", "text": "s"}],
        True,
    ),
    ("copilot", {"type": "system.message", "data": {}}, False, [], False),
    (
        "copilot",
        {"type": "tool.execution_start", "data": {}},
        False,
        [{"kind": "tool_call", "tool_name": "", "tool_input": None, "tool_use_id": ""}],
        False,
    ),
    (
        "copilot",
        {"type": "tool.execution_start", "data": {"toolName": "view", "arguments": {"p": 1}, "toolCallId": "k"}},
        False,
        [{"kind": "tool_call", "tool_name": "view", "tool_input": {"p": 1}, "tool_use_id": "k"}],
        False,
    ),
    (
        "copilot",
        {"type": "tool.execution_complete", "data": {}},
        False,
        [{"kind": "tool_result", "tool_result": None, "tool_use_id": "", "is_error": False}],
        False,
    ),
    (
        "copilot",
        {
            "type": "tool.execution_complete",
            "data": {"success": False, "error": {"message": "boom"}, "toolCallId": "k"},
        },
        False,
        [{"kind": "tool_result", "tool_result": "boom", "tool_use_id": "k", "is_error": True}],
        False,
    ),
    (
        "copilot",
        {"type": "tool.execution_complete", "data": {"success": False, "error": {}}},
        False,
        [{"kind": "tool_result", "tool_result": "", "tool_use_id": "", "is_error": True}],
        False,
    ),
    (
        "copilot",
        {"type": "tool.execution_complete", "data": {"success": False, "error": "e", "result": {"content": "r"}}},
        False,
        [{"kind": "tool_result", "tool_result": "r", "tool_use_id": "", "is_error": True}],
        False,
    ),
    (
        "copilot",
        {
            "type": "tool.execution_complete",
            "data": {"error": {"message": "x"}, "result": {"content": "", "detailedContent": "d"}},
        },
        False,
        [{"kind": "tool_result", "tool_result": "d", "tool_use_id": "", "is_error": False}],
        False,
    ),
    (
        "copilot",
        {
            "type": "tool.execution_complete",
            "data": {"result": {"contents": [{"text": "a"}, {"text": ""}, "x", {"type": "t"}, {"text": "b"}]}},
        },
        False,
        [{"kind": "tool_result", "tool_result": "a\nb", "tool_use_id": "", "is_error": False}],
        False,
    ),
    (
        "copilot",
        {"type": "tool.execution_complete", "data": {"result": {"content": 3, "contents": [{"text": ""}]}}},
        False,
        [
            {
                "kind": "tool_result",
                "tool_result": {"content": 3, "contents": [{"text": ""}]},
                "tool_use_id": "",
                "is_error": False,
            }
        ],
        False,
    ),
    (
        "copilot",
        {"type": "tool.execution_complete", "data": {"result": {"contents": "no"}}},
        False,
        [{"kind": "tool_result", "tool_result": {"contents": "no"}, "tool_use_id": "", "is_error": False}],
        False,
    ),
    (
        "copilot",
        {"type": "tool.execution_complete", "data": {"result": "plain"}},
        False,
        [{"kind": "tool_result", "tool_result": "plain", "tool_use_id": "", "is_error": False}],
        False,
    ),
    ("copilot", {"type": "assistant.usage", "data": {}}, True, [{"kind": "token_usage"}], True),
    ("copilot", {"type": "session.task_complete", "data": {}}, False, [{"kind": "turn_complete", "text": ""}], False),
    (
        "copilot",
        {"type": "session.task_complete", "data": {"summary": "s"}},
        False,
        [{"kind": "turn_complete", "text": "s"}],
        False,
    ),
    ("copilot", {"type": "session.task_complete", "data": {"summary": "s"}}, True, [], False),
    ("copilot", {"type": "session.start", "data": {}}, True, [{"kind": "meta"}], True),
    ("copilot", {"type": "hook.start", "data": {"content": "h"}}, True, [], True),
]


@pytest.mark.parametrize(("fmt", "entry", "seen", "expected", "seen_after"), DECODE_CASES)
def test_decoder_records_exactly(fmt, entry, seen, expected, seen_after):
    state = TurnState(assistant_seen=seen)
    assert list(DECODERS[fmt](entry, state)) == [dict(rec, raw=entry) for rec in expected]
    assert state.assistant_seen is seen_after


@pytest.mark.parametrize(
    "prefix", ["session.", "user.", "assistant.", "tool.", "system.", "hook.", "subagent.", "mcp.", "permission."]
)
def test_every_copilot_prefix_is_recognised(prefix):
    assert is_copilot_type(f"{prefix}event")
    assert not is_copilot_type(prefix.rstrip("."))


def test_non_string_type_is_not_copilot():
    assert not is_copilot_type(None)


@pytest.mark.parametrize("etype", ["session_meta", "response_item", "event_msg", "turn_context", "world_state"])
def test_every_codex_type_detects_codex(tmp_path, etype):
    path = _write(tmp_path, "rollout.jsonl", [{"type": etype, "payload": {}}])
    assert detect_transcript_format(path) == "codex"
    assert etype in CODEX_TYPES
