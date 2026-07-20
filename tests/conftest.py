"""Shared test fixtures for agentihooks."""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))


@pytest.fixture(autouse=True)
def _isolate_real_user_paths(tmp_path, monkeypatch):
    """Point install.py's real-home globals at tmp for every test in the suite.

    `CLAUDE_HOME`, `AGENTIHOOKS_STATE_DIR` and `STATE_JSON` are module-level and
    resolve to the developer's actual `~/.claude` and `~/.agentihooks`. They are
    reached transitively — `_install_system_prompt` calls
    `_record_managed_claude_md`, which calls `_save_state` — so a test that never
    mentions them still writes to real files. This has already destroyed a real
    `~/.claude/CLAUDE.md` once.

    Autouse and suite-wide on purpose: opting in per file is how the gap
    reappeared last time.
    """
    try:
        import install
    except Exception:  # suite runs fine without the installer importable
        yield
        return

    claude_home = tmp_path / "_home" / ".claude"
    claude_home.mkdir(parents=True)
    state_dir = tmp_path / "_home" / ".agentihooks"
    state_dir.mkdir(parents=True)

    monkeypatch.setattr(install, "CLAUDE_HOME", claude_home, raising=False)
    monkeypatch.setattr(install, "AGENTIHOOKS_STATE_DIR", state_dir, raising=False)
    monkeypatch.setattr(install, "STATE_JSON", state_dir / "state.json", raising=False)

    real_home = Path.home()
    for name in ("CLAUDE_HOME", "AGENTIHOOKS_STATE_DIR", "STATE_JSON"):
        value = Path(getattr(install, name))
        assert real_home not in value.parents and value != real_home, (
            f"install.{name} still resolves under the real home ({value}) — refusing to run"
        )
    yield


@pytest.fixture
def mock_env():
    """Provide a clean environment for tests."""
    env = {
        "CLAUDE_HOOK_LOG_ENABLED": "true",
        "CLAUDE_HOOK_LOG_FILE": "/tmp/test-hooks.log",
    }
    with patch.dict(os.environ, env, clear=False):
        yield env


@pytest.fixture
def tmp_log_file(tmp_path):
    """Provide a temporary log file path."""
    return tmp_path / "test.log"


@pytest.fixture
def sample_transcript_entry():
    """A sample transcript JSONL entry."""
    return {
        "type": "user",
        "message": {"content": [{"type": "text", "text": "Hello Claude"}]},
        "timestamp": "2026-01-15T10:00:00Z",
        "uuid": "test-uuid-001",
    }


@pytest.fixture
def sample_tool_use_event():
    """A sample PreToolUse hook event."""
    return {
        "session_id": "test-session",
        "tool_name": "Write",
        "tool_input": {"file_path": "/tmp/test.txt", "content": "hello"},
    }
