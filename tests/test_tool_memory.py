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


@pytest.mark.parametrize("field", ["error", "input", "tool", "session", "ts"])
def test_memory_rejects_credentials_before_storage_and_replay(tmp_path, field):
    from hooks import tool_memory

    credential = "synthetic" + "-credential"
    entry = {"ts": "2026-10-09", "tool": "Bash", "error": "Error: unavailable", "input": "", "session": ""}
    entry[field] = "redis://:" + credential + "@localhost:6379"
    memory = tmp_path / "memory.ndjson"
    with patch.object(tool_memory, "MEMORY_PATH", memory), patch("hooks.common.inject_banner") as banner:
        tool_memory._append_entry(entry)
        assert not memory.exists(), "credential entry was stored"
        tool_memory._append_entries([entry])
        assert not memory.exists(), "credential batch was stored"
        memory.write_text(json.dumps(entry) + "\n")
        tool_memory.inject_memory()
        banner.assert_not_called()


@pytest.mark.parametrize("field", ["error", "input", "tool", "session", "ts"])
def test_legacy_credentials_never_replay(tmp_path, field):
    from hooks import tool_memory

    entry = {"ts": "2026-10-09", "tool": "Bash", "error": "Error: unavailable", "input": "", "session": ""}
    entry[field] = "redis://:" + "synthetic" + "-credential@localhost:6379"
    memory = tmp_path / "memory.ndjson"
    memory.write_text(json.dumps(entry) + "\n")
    with patch.object(tool_memory, "MEMORY_PATH", memory), patch("hooks.common.inject_banner") as banner:
        tool_memory.inject_memory()
        banner.assert_not_called()


@pytest.mark.parametrize(
    "text",
    [
        "REDIS_PASSWORD=" + "x",
        "PASSWORD=" + "synthetic.credential",
        "postgresql://user:" + "synthetic" + "-credential@localhost:5432",
        "mongodb+srv://user:" + "synthetic" + "-credential@localhost:27017",
    ],
)
def test_memory_drops_short_assignments_and_connection_aliases(tmp_path, text):
    from hooks import tool_memory

    memory = tmp_path / "memory.ndjson"
    with patch.object(tool_memory, "MEMORY_PATH", memory):
        tool_memory.record_error(
            {
                "tool_name": "Bash",
                "tool_response": {"is_error": True, "content": "Error: " + text},
            }
        )
        assert not memory.exists(), "credential error was stored"
    memory.write_text(json.dumps({"tool": "Bash", "error": text}) + "\n")
    with patch.object(tool_memory, "MEMORY_PATH", memory), patch("hooks.common.inject_banner") as banner:
        tool_memory.inject_memory()
        banner.assert_not_called()


@pytest.mark.parametrize("source", ["output", "input"])
def test_record_error_checks_credentials_before_truncation(tmp_path, source):
    from hooks import tool_memory

    credential = "synthetic" + "-credential"
    text = "Error: " + "x" * 190 + " redis://:" + credential + "@localhost:6379"
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": text if source == "input" else "safe command"},
        "tool_response": {"is_error": True, "content": text if source == "output" else "Error: failed"},
    }
    memory = tmp_path / "memory.ndjson"
    with patch.object(tool_memory, "MEMORY_PATH", memory):
        tool_memory.record_error(payload)
        assert not memory.exists(), "credential output was stored"


@pytest.mark.parametrize("source", ["output", "input"])
def test_transcript_checks_credentials_before_truncation(tmp_path, source):
    from hooks import tool_memory

    text = "Error: " + "x" * 190 + " redis://:" + "synthetic" + "-credential@localhost:6379"
    records = [
        {
            "kind": "tool_call",
            "tool_use_id": "call",
            "tool_name": "Bash",
            "tool_input": {"command": text if source == "input" else "safe command"},
        },
        {
            "kind": "tool_result",
            "tool_use_id": "call",
            "is_error": True,
            "tool_result": text if source == "output" else "Error: failed",
        },
    ]
    memory = tmp_path / "memory.ndjson"
    with (
        patch.object(tool_memory, "MEMORY_PATH", memory),
        patch("hooks.memory.transcript_reader.iter_transcript_records", return_value=iter(records)),
    ):
        tool_memory.scan_transcript({"transcript_path": "synthetic.jsonl"})
        assert not memory.exists(), "transcript credential was stored"


def test_safe_entries_survive_contaminated_batch_and_rotation(tmp_path):
    from hooks import tool_memory

    safe = {"tool": "Bash", "error": "Error: safe guidance", "input": "safe command"}
    unsafe = {"tool": "Bash", "error": "redis://:" + "synthetic" + "-credential@localhost:6379"}
    memory = tmp_path / "memory.ndjson"
    memory.write_text(json.dumps(unsafe) + "\n")
    with patch.object(tool_memory, "MEMORY_PATH", memory), patch.object(tool_memory, "MAX_ENTRIES", 1):
        tool_memory._append_entries([unsafe, safe])
        assert memory.read_text().splitlines() == [json.dumps(safe, separators=(",", ":"))]
        tool_memory._append_entry(safe)
        assert memory.read_text().splitlines() == [json.dumps(safe, separators=(",", ":"))]
        with patch("hooks.common.inject_banner") as banner:
            tool_memory.inject_memory()
        assert banner.call_count == 1
        assert "safe guidance" in banner.call_args.args[1]


@pytest.mark.parametrize(
    "text",
    [
        "PASSWORD=" + "synthetic" + "-credential",
        "redis://:" + "x" + "@localhost:6379 # nosecret",
        "rediss://user:" + "synthetic" + "-credential@localhost:6379",
        "ghp_" + "a" * 36,
        "Bearer " + "a" * 24,
    ],
)
def test_memory_filters_secret_forms_with_scanning_disabled(tmp_path, text):
    from hooks import tool_memory

    entry = {"tool": "Bash", "error": "Error: " + text}
    memory = tmp_path / "memory.ndjson"
    with patch.object(tool_memory, "MEMORY_PATH", memory), patch("hooks.config.SECRETS_MODE", "off"):
        tool_memory._append_entry(entry)
        assert not memory.exists(), "credential entry was stored"


@pytest.mark.parametrize(
    "entry",
    [
        {
            "error": "Error: unavailable",
            "metadata": {"redis://:" + "synthetic" + "-credential@localhost:6379": "failed"},
        },
        {"error": "Error: unavailable", "metadata": {"note": "first line\nPASSWORD=" + "x"}},
        {"error": "Error: unavailable", "metadata": {"PASSWORD": "x"}},
        {"error": "Error: unavailable", "metadata": ("PASSWORD=" + "x",)},
    ],
)
def test_memory_scans_nested_keys_and_multiline_values(tmp_path, entry):
    from hooks import tool_memory

    memory = tmp_path / "memory.ndjson"
    with patch.object(tool_memory, "MEMORY_PATH", memory), patch("hooks.common.inject_banner") as banner:
        tool_memory._append_entry(entry)
        assert not memory.exists(), "nested credential entry was stored"
        memory.write_text(json.dumps(entry) + "\n")
        tool_memory.inject_memory()
        banner.assert_not_called()


@pytest.mark.parametrize("empty", ["", None])
def test_memory_preserves_separate_safe_fields(tmp_path, empty):
    from hooks import tool_memory

    entry = {
        "tool": "Bash",
        "error": "Error: unavailable",
        "input": "PASSWORD=",
        "session": "safe session",
        "metadata": {"PASSWORD": empty},
    }
    memory = tmp_path / "memory.ndjson"
    with patch.object(tool_memory, "MEMORY_PATH", memory):
        tool_memory._append_entry(entry)
        assert tool_memory._read_entries() == [entry]


def test_transcript_preserves_safe_errors_after_credentials(tmp_path):
    from hooks import tool_memory

    records = [
        {
            "kind": "tool_result",
            "is_error": True,
            "tool_result": "Error: redis://:" + "synthetic" + "-credential@localhost:6379",
        },
        {"kind": "tool_result", "is_error": True, "tool_result": "Error: unavailable"},
    ]
    memory = tmp_path / "memory.ndjson"
    with (
        patch.object(tool_memory, "MEMORY_PATH", memory),
        patch("hooks.memory.transcript_reader.iter_transcript_records", return_value=iter(records)),
    ):
        tool_memory.scan_transcript({"transcript_path": "synthetic.jsonl"})
        entries = tool_memory._read_entries()
        assert len(entries) == 1
        assert entries[0]["error"] == "Error: unavailable"


@pytest.mark.parametrize("value", [1234, 12.5, True])
def test_memory_drops_scalar_credential_fields(tmp_path, value):
    from hooks import tool_memory

    entry = {"tool": "Bash", "error": "Error: unavailable", "metadata": {"PASSWORD": value}}
    memory = tmp_path / "memory.ndjson"
    with patch.object(tool_memory, "MEMORY_PATH", memory), patch("hooks.common.inject_banner") as banner:
        tool_memory._append_entry(entry)
        assert not memory.exists(), "scalar credential field was stored"
        memory.write_text(json.dumps(entry) + "\n")
        tool_memory.inject_memory()
        banner.assert_not_called()


def test_memory_preserves_guidance_after_empty_assignment(tmp_path):
    from hooks import tool_memory

    entry = {"tool": "Bash", "error": "PASSWORD=\nsafe guidance"}
    memory = tmp_path / "memory.ndjson"
    with patch.object(tool_memory, "MEMORY_PATH", memory):
        tool_memory._append_entry(entry)
        assert tool_memory._read_entries() == [entry]


def test_memory_rejects_multiline_bearer_credentials(tmp_path):
    from hooks import tool_memory

    entry = {"tool": "Bash", "error": "Error: Bearer\n" + "a" * 24}
    memory = tmp_path / "memory.ndjson"
    with patch.object(tool_memory, "MEMORY_PATH", memory), patch("hooks.common.inject_banner") as banner:
        tool_memory._append_entry(entry)
        assert not memory.exists(), "multiline credential entry was stored"
        memory.write_text(json.dumps(entry) + "\n")
        tool_memory.inject_memory()
        banner.assert_not_called()


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
