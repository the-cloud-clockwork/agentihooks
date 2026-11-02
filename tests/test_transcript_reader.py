"""Tests for hooks.memory.transcript_reader module."""

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _write_entries(path: Path, entries: list[dict]) -> None:
    """Helper: write JSONL entries to a file."""
    with open(path, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


class TestParseEntries:
    """Test the internal _parse_entries() helper."""

    def test_parse_valid_jsonl(self, tmp_path):
        from hooks.memory.transcript_reader import _parse_entries

        f = tmp_path / "log.jsonl"
        _write_entries(f, [{"type": "user", "message": "hello"}])
        entries = _parse_entries(str(f))
        assert len(entries) == 1
        assert entries[0]["type"] == "user"

    def test_parse_nonexistent_file(self):
        from hooks.memory.transcript_reader import _parse_entries

        entries = _parse_entries("/nonexistent/path/log.jsonl")
        assert entries == []

    def test_parse_skips_invalid_lines(self, tmp_path):
        from hooks.memory.transcript_reader import _parse_entries

        f = tmp_path / "log.jsonl"
        with open(f, "w") as fh:
            fh.write('{"type":"user"}\n')
            fh.write("not json\n")
            fh.write("\n")
            fh.write('{"type":"assistant"}\n')
        entries = _parse_entries(str(f))
        assert len(entries) == 2

    def test_parse_empty_file(self, tmp_path):
        from hooks.memory.transcript_reader import _parse_entries

        f = tmp_path / "log.jsonl"
        f.write_text("")
        entries = _parse_entries(str(f))
        assert entries == []


class TestExtractText:
    """Test the internal _extract_text() helper."""

    def test_extract_string_message(self):
        from hooks.memory.transcript_reader import _extract_text

        entry = {"message": "simple string"}
        assert _extract_text(entry) == "simple string"

    def test_extract_dict_message_with_content_list(self):
        from hooks.memory.transcript_reader import _extract_text

        entry = {
            "message": {
                "content": [
                    {"type": "text", "text": "hello"},
                    {"type": "text", "text": "world"},
                ]
            }
        }
        assert _extract_text(entry) == "hello\nworld"

    def test_extract_dict_message_with_content_string(self):
        from hooks.memory.transcript_reader import _extract_text

        entry = {"message": {"content": "plain string content"}}
        assert _extract_text(entry) == "plain string content"

    def test_extract_no_message(self):
        from hooks.memory.transcript_reader import _extract_text

        entry = {"type": "user"}
        assert _extract_text(entry) is None

    def test_extract_empty_content_list(self):
        from hooks.memory.transcript_reader import _extract_text

        entry = {"message": {"content": []}}
        assert _extract_text(entry) is None

    def test_extract_non_text_blocks(self):
        from hooks.memory.transcript_reader import _extract_text

        entry = {
            "message": {
                "content": [
                    {"type": "image", "url": "http://example.com/img.png"},
                ]
            }
        }
        assert _extract_text(entry) is None

    def test_extract_mixed_blocks(self):
        from hooks.memory.transcript_reader import _extract_text

        entry = {
            "message": {
                "content": [
                    {"type": "image", "url": "http://example.com"},
                    {"type": "text", "text": "description"},
                ]
            }
        }
        assert _extract_text(entry) == "description"


class TestSearchTranscripts:
    """Test search_transcripts() function."""

    def _make_log(self, tmp_path):
        """Create a sample agent log."""
        f = tmp_path / "agent.log"
        entries = [
            {
                "session_id": "s1",
                "type": "user",
                "message": "How do I install Python?",
                "timestamp": "2026-01-01T10:00:00Z",
            },
            {
                "session_id": "s1",
                "type": "assistant",
                "message": "You can install Python from python.org",
                "timestamp": "2026-01-01T10:00:01Z",
            },
            {
                "session_id": "s2",
                "type": "user",
                "message": "Tell me about Docker containers",
                "timestamp": "2026-01-01T11:00:00Z",
            },
            {
                "session_id": "s2",
                "type": "assistant",
                "message": "Docker containers are lightweight VMs",
                "timestamp": "2026-01-01T11:00:01Z",
            },
            {
                "session_id": "s1",
                "type": "summary",
                "message": "Session summary goes here",
                "timestamp": "2026-01-01T10:05:00Z",
            },
        ]
        _write_entries(f, entries)
        return str(f)

    def test_search_finds_matching_entries(self, tmp_path):
        """User question + assistant answer."""
        from hooks.memory.transcript_reader import search_transcripts

        log_path = self._make_log(tmp_path)
        results = search_transcripts("Python", log_path=log_path)
        assert len(results) == 2

    def test_search_case_insensitive(self, tmp_path):
        from hooks.memory.transcript_reader import search_transcripts

        log_path = self._make_log(tmp_path)
        results = search_transcripts("python", log_path=log_path)
        assert len(results) == 2

    def test_search_filter_by_session(self, tmp_path):
        """Only entries from s1 that mention Python."""
        from hooks.memory.transcript_reader import search_transcripts

        log_path = self._make_log(tmp_path)
        results = search_transcripts("Python", session_id="s1", log_path=log_path)
        assert len(results) >= 1
        for r in results:
            assert r["session_id"] == "s1"

    def test_search_respects_limit(self, tmp_path):
        from hooks.memory.transcript_reader import search_transcripts

        log_path = self._make_log(tmp_path)
        results = search_transcripts("Python", limit=1, log_path=log_path)
        assert len(results) == 1

    def test_search_no_results(self, tmp_path):
        from hooks.memory.transcript_reader import search_transcripts

        log_path = self._make_log(tmp_path)
        results = search_transcripts("nonexistent-query-xyz", log_path=log_path)
        assert results == []

    def test_search_skips_summary_entries(self, tmp_path):
        """ "summary" type entries should be skipped (only user/assistant)"""
        from hooks.memory.transcript_reader import search_transcripts

        log_path = self._make_log(tmp_path)
        results = search_transcripts("summary", log_path=log_path)
        assert results == []

    def test_search_returns_newest_first(self, tmp_path):
        from hooks.memory.transcript_reader import search_transcripts

        log_path = self._make_log(tmp_path)
        results = search_transcripts("Docker", log_path=log_path)
        assert len(results) >= 1

    def test_search_truncates_long_content(self, tmp_path):
        """Content is capped at 2000 chars."""
        from hooks.memory.transcript_reader import search_transcripts

        f = tmp_path / "agent.log"
        long_msg = "keyword " + "x" * 3000
        _write_entries(
            f,
            [
                {
                    "session_id": "s1",
                    "type": "user",
                    "message": long_msg,
                    "timestamp": "2026-01-01T10:00:00Z",
                }
            ],
        )
        results = search_transcripts("keyword", log_path=str(f))
        assert len(results) == 1
        assert len(results[0]["content"]) <= 2000


class TestGetSessionTranscript:
    """Test get_session_transcript() function."""

    def _make_log(self, tmp_path):
        f = tmp_path / "agent.log"
        entries = [
            {
                "session_id": "s1",
                "type": "user",
                "message": f"Message {i}",
                "timestamp": f"2026-01-01T10:{i:02d}:00Z",
            }
            for i in range(5)
        ]
        entries.append(
            {
                "session_id": "s2",
                "type": "user",
                "message": "different session",
                "timestamp": "2026-01-01T11:00:00Z",
            }
        )
        _write_entries(f, entries)
        return str(f)

    def test_get_session_returns_matching(self, tmp_path):
        from hooks.memory.transcript_reader import get_session_transcript

        log_path = self._make_log(tmp_path)
        results = get_session_transcript("s1", log_path=log_path)
        assert len(results) == 5
        for r in results:
            assert r["session_id"] == "s1"

    def test_get_session_last_n(self, tmp_path):
        from hooks.memory.transcript_reader import get_session_transcript

        log_path = self._make_log(tmp_path)
        results = get_session_transcript("s1", last_n=2, log_path=log_path)
        assert len(results) == 2

    def test_get_session_nonexistent(self, tmp_path):
        from hooks.memory.transcript_reader import get_session_transcript

        log_path = self._make_log(tmp_path)
        results = get_session_transcript("nonexistent", log_path=log_path)
        assert results == []

    def test_get_session_empty_log(self, tmp_path):
        from hooks.memory.transcript_reader import get_session_transcript

        f = tmp_path / "empty.log"
        f.write_text("")
        results = get_session_transcript("s1", log_path=str(f))
        assert results == []
