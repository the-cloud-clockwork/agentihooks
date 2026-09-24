"""Tests for hooks.tool_memory module."""

import json
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


class TestToolMemory:
    """Test tool memory system."""

    def test_import(self):
        """Module can be imported."""
        from hooks.tool_memory import inject_memory, record_error, scan_transcript

        assert callable(inject_memory)
        assert callable(record_error)
        assert callable(scan_transcript)

    def test_record_error_creates_entry(self, tmp_path):
        """record_error() stores error data."""
        mem_file = tmp_path / "tool_memory.ndjson"
        with patch("hooks.tool_memory.MEMORY_PATH", mem_file):
            from hooks.tool_memory import record_error

            # record_error takes a payload dict
            payload = {
                "tool_name": "Write",
                "tool_input": {"file_path": "/tmp/test.txt"},
                "tool_response": {"is_error": True, "content": "File not found"},
                "session_id": "test-session",
            }
            record_error(payload)
            # Verify it wrote an entry
            assert mem_file.exists()
            lines = mem_file.read_text().strip().split("\n")
            assert len(lines) == 1
            entry = json.loads(lines[0])
            assert entry["tool"] == "Write"
            assert "File not found" in entry["error"]


class TestIsErrorExplicitStatus:
    def test_file_tools_trust_only_explicit_flags(self):
        from hooks.tool_memory import _is_error, strict_detection

        echoed = {"type": "create", "filePath": "/x.py", "content": "raise TimeoutError('not found')"}
        for tool in ("Write", "Edit", "MultiEdit", "NotebookEdit", "Read"):
            assert strict_detection(tool)
            assert _is_error(echoed, strict=strict_detection(tool)) == (False, "")
        assert _is_error({"is_error": True, "content": "String to replace not found"}, strict=True)[0]
        assert not strict_detection("Bash")

    def test_copilot_result_type(self):
        from hooks.tool_memory import _is_error

        assert _is_error({"resultType": "failure", "textResultForLlm": "boom"}, strict=True) == (True, "boom")
        assert _is_error({"resultType": "denied", "textResultForLlm": "error: blocked by hook"}) == (False, "")
