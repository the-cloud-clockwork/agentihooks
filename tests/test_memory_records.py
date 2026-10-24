"""SV2-SES-01: per-entry record normalization shared by the whole-file reader and incremental consumers."""

import json
from pathlib import Path

import pytest

from hooks.memory.records import DECODERS, TurnState, normalize_entries
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
