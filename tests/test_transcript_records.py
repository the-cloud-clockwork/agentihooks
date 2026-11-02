"""Tests for the unified transcript-record API (claude / codex / copilot)."""

from pathlib import Path

from hooks.memory.transcript_reader import detect_transcript_format, iter_transcript_records

FIXTURES = Path(__file__).parent / "fixtures"
CLAUDE = FIXTURES / "claude_transcript_sample.jsonl"
CODEX = FIXTURES / "codex_rollout_sample.jsonl"
COPILOT = FIXTURES / "copilot_events_sample.jsonl"


class TestDetection:
    def test_formats_detected(self):
        assert detect_transcript_format(CLAUDE) == "claude"
        assert detect_transcript_format(CODEX) == "codex"
        assert detect_transcript_format(COPILOT) == "copilot"

    def test_missing_file_yields_nothing(self, tmp_path):
        assert list(iter_transcript_records(tmp_path / "nope.jsonl")) == []

    def test_corrupt_first_line_still_detects_codex(self, tmp_path):
        """A rollout read mid-write can have a truncated/corrupt first line. detection must look past it instead of
        misdetecting as claude.
        """
        p = tmp_path / "corrupt_first.jsonl"
        p.write_text(
            "not valid json{{{\n"
            '{"type": "response_item", "payload": {"type": "message", "id": "m1", '
            '"role": "user", "content": [{"type": "input_text", "text": "hi"}]}}\n'
        )
        assert detect_transcript_format(p) == "codex"
        recs = list(iter_transcript_records(p))
        assert any(r["kind"] == "user_text" for r in recs)

    def test_world_state_first_line_detects_codex(self, tmp_path):
        p = tmp_path / "world_state_first.jsonl"
        p.write_text(
            '{"type": "world_state", "payload": {}}\n'
            '{"type": "response_item", "payload": {"type": "message", "id": "m1", '
            '"role": "user", "content": [{"type": "input_text", "text": "hi"}]}}\n'
        )
        assert detect_transcript_format(p) == "codex"

    def test_invalid_utf8_bytes_no_exception(self, tmp_path):
        p = tmp_path / "bad_utf8.jsonl"
        p.write_bytes(b'{"type": "user", "message": {"content": [{"type": "text", "text": "bad byte \xff here"}]}}\n')
        recs = list(iter_transcript_records(p))
        assert recs and recs[0]["kind"] == "user_text"


class TestSameScenarioBothFormats:
    """Both fixtures encode the same conceptual scenario: user turn, assistant
    text with an embedded @lesson marker, one tool call, one tool result."""

    def test_claude_records(self):
        recs = list(iter_transcript_records(CLAUDE))
        kinds = [r["kind"] for r in recs]
        assert "user_text" in kinds and "assistant_text" in kinds
        assert "tool_call" in kinds and "tool_result" in kinds
        call = next(r for r in recs if r["kind"] == "tool_call")
        result = next(r for r in recs if r["kind"] == "tool_result")
        assert call["tool_name"] == "Bash"
        assert call["tool_use_id"] == result["tool_use_id"] == "tu1"
        assert result["is_error"] is True
        assistant = "\n".join(r["text"] for r in recs if r["kind"] == "assistant_text")
        assert "@lesson" in assistant

    def test_codex_records(self):
        """The fixture's task_complete repeats the same reply the response_item message already yielded as assistant_text
        — turn_complete must be suppressed so consumers don't double-count the turn.

        Injected context (role=developer) must NOT masquerade as assistant text.
        """
        recs = list(iter_transcript_records(CODEX))
        kinds = [r["kind"] for r in recs]
        assert "meta" in kinds and "token_usage" in kinds
        assert "turn_complete" not in kinds
        assert kinds.count("assistant_text") == 1
        system = [r for r in recs if r["kind"] == "system_text"]
        assert any("CONTEXT INJECTION" in r["text"] for r in system)
        assistant = "\n".join(r["text"] for r in recs if r["kind"] == "assistant_text")
        assert "@lesson" in assistant and "CONTEXT INJECTION" not in assistant
        call = next(r for r in recs if r["kind"] == "tool_call")
        result = next(r for r in recs if r["kind"] == "tool_result")
        assert call["tool_name"] == "shell"
        assert call["tool_use_id"] == result["tool_use_id"] == "c1"

    def test_codex_truncated_rollout_falls_back_to_turn_complete(self, tmp_path):
        """No response_item assistant message landed before task_complete — the reader must still surface the turn via
        the fallback text.
        """
        p = tmp_path / "truncated.jsonl"
        p.write_text(
            "\n".join(
                [
                    '{"type": "session_meta", "payload": {"id": "s2", "cwd": "/w"}}',
                    '{"type": "response_item", "payload": {"type": "message", "id": "m1", '
                    '"role": "user", "content": [{"type": "input_text", "text": "fix it"}]}}',
                    '{"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t1", '
                    '"last_agent_message": "Truncated response"}}',
                ]
            )
            + "\n"
        )
        recs = list(iter_transcript_records(p))
        kinds = [r["kind"] for r in recs]
        assert kinds.count("assistant_text") == 0
        assert kinds.count("turn_complete") == 1
        turn_complete = next(r for r in recs if r["kind"] == "turn_complete")
        assert turn_complete["text"] == "Truncated response"

    def test_claude_bare_string_message_yields_text(self, tmp_path):
        """Some claude entries carry `message` as a plain string rather than {content: [...]} — a marker inside it must
        not be dropped.
        """
        p = tmp_path / "bare_string.jsonl"
        p.write_text(
            '{"type": "assistant", "message": "Done.\\n<!-- @lesson -->\\nbare string message\\n<!-- @/lesson -->"}\n'
        )
        recs = list(iter_transcript_records(p))
        assert len(recs) == 1
        assert recs[0]["kind"] == "assistant_text"
        assert "@lesson" in recs[0]["text"]


class TestConsumers:
    def test_brain_writer_finds_markers_in_codex_rollout(self):
        from hooks.context.brain_writer_hook import _parse_transcript_for_markers

        markers = _parse_transcript_for_markers(str(CODEX), max_markers=5)
        assert len(markers) == 1
        assert markers[0]["type"] == "lesson"
        assert "response_item" in markers[0]["content"]

    def test_brain_writer_finds_markers_in_claude_transcript(self):
        from hooks.context.brain_writer_hook import _parse_transcript_for_markers

        markers = _parse_transcript_for_markers(str(CLAUDE), max_markers=5)
        assert len(markers) == 1
        assert markers[0]["type"] == "lesson"

    def test_tool_memory_scan_claude_errors(self):
        from hooks.tool_memory import _scan_transcript_for_errors

        entries = _scan_transcript_for_errors(str(CLAUDE), session_id="s")
        assert len(entries) == 1
        assert entries[0]["tool"] == "Bash"
        assert "command not found" in entries[0]["error"]

    def test_tool_memory_scan_codex_no_false_positives(self):
        """Codex outputs carry no is_error flag — scan must stay silent, not guess."""
        from hooks.tool_memory import _scan_transcript_for_errors

        assert _scan_transcript_for_errors(str(CODEX), session_id="s") == []

    def test_auto_save_reads_both(self):
        from hooks.memory.auto_save import _read_transcript

        for fixture in (CLAUDE, CODEX):
            entries = _read_transcript(fixture)
            types = {e["type"] for e in entries}
            assert {"user", "assistant"} <= types, fixture.name


class TestCopilotRecords:
    """Fixture is derived from the shipped session-events.schema.json, not from
    a captured session — copilot only writes events.jsonl on an authenticated
    turn. Replace it with a captured log when one exists (COPILOT-COMPAT §6)."""

    def _records(self):
        return list(iter_transcript_records(COPILOT))

    def test_user_and_assistant_text_extracted(self):
        recs = self._records()
        assert [r["text"] for r in recs if r["kind"] == "user_text"] == ["list the files", "thanks"]
        assert [r["text"] for r in recs if r["kind"] == "assistant_text"] == ["I'll list them."]

    def test_tool_call_and_result_correlate_by_id(self):
        recs = self._records()
        calls = {r["tool_use_id"]: r for r in recs if r["kind"] == "tool_call"}
        results = {r["tool_use_id"]: r for r in recs if r["kind"] == "tool_result"}
        assert calls["call_1"]["tool_name"] == "shell"
        assert set(calls) == set(results)

    def test_failed_tool_flagged_with_message(self):
        """Copilot states success explicitly — unlike codex, errors are visible."""
        recs = self._records()
        failed = [r for r in recs if r["kind"] == "tool_result" and r["is_error"]]
        assert len(failed) == 1
        assert "ENOENT" in failed[0]["tool_result"]

    def test_ephemeral_streaming_deltas_excluded(self):
        """Counting deltas would multiply every reply by its chunk count."""
        recs = self._records()
        assert not any("partial text" in str(r.get("text", "")) for r in recs)

    def test_task_complete_only_fills_a_turn_with_no_assistant_message(self):
        recs = self._records()
        completes = [r for r in recs if r["kind"] == "turn_complete"]
        assert [r["text"] for r in completes] == ["Acknowledged."]

    def test_usage_event_yields_token_record(self):
        assert any(r["kind"] == "token_usage" for r in self._records())

    def test_session_metrics_count_copilot_turns(self):
        from hooks.hook_manager import parse_transcript_metrics

        metrics = parse_transcript_metrics(str(COPILOT))
        assert metrics["num_turns"] == 2
        assert metrics["last_response"] == "Acknowledged."


COPILOT_REAL = FIXTURES / "copilot_events_real.jsonl"


class TestCopilotRealCapture:
    """Captured from a live authenticated copilot v1.0.80 session (2026-08-19),
    with the system prompt elided and the synthetic test key scrubbed. This is
    the ground-truth counterpart to the schema-derived sample fixture."""

    def _records(self):
        return list(iter_transcript_records(COPILOT_REAL))

    def test_format_detected(self):
        assert detect_transcript_format(COPILOT_REAL) == "copilot"

    def test_tool_calls_carry_copilot_names(self):
        names = {r["tool_name"] for r in self._records() if r["kind"] == "tool_call"}
        assert {"glob", "bash", "edit"} <= names

    def test_hook_injected_context_is_not_a_user_turn(self):
        recs = self._records()
        user_texts = [r["text"] for r in recs if r["kind"] == "user_text"]
        assert len(user_texts) == 1, "source=system user.message entries must not count as user turns"
        assert any(r["kind"] == "system_text" and "preToolUse hook" in r["text"] for r in recs), (
            "injected context should surface as system_text, not vanish"
        )

    def test_session_metrics_on_real_capture(self):
        from hooks.hook_manager import parse_transcript_metrics

        metrics = parse_transcript_metrics(str(COPILOT_REAL))
        assert metrics["num_turns"] == 1
        assert metrics["last_response"] == "DONE"
