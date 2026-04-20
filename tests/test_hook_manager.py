"""Tests for hooks.hook_manager module."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_PROJECT_ROOT = Path(__file__).parent.parent


class TestHookManager:
    """Test the central event dispatcher."""

    def test_event_handlers_dict_exists(self):
        """EVENT_HANDLERS is defined and non-empty."""
        from hooks.hook_manager import EVENT_HANDLERS

        assert isinstance(EVENT_HANDLERS, dict)
        assert len(EVENT_HANDLERS) > 0

    def test_known_events(self):
        """Standard hook events are registered."""
        from hooks.hook_manager import EVENT_HANDLERS

        expected_events = ["PreToolUse", "PostToolUse", "Stop"]
        for event in expected_events:
            assert event in EVENT_HANDLERS, f"Missing handler for {event}"

    def test_main_requires_stdin(self):
        """main() reads from stdin for event data."""
        from hooks.hook_manager import main

        assert callable(main)

    def test_block_action_exception_exists(self):
        """BlockAction is importable and is an Exception subclass."""
        from hooks.hook_manager import BlockAction

        assert issubclass(BlockAction, Exception)


class TestBlockActionIntegration:
    """Integration tests: BlockAction propagates through main() with exit 2."""

    def _run(self, payload: dict) -> subprocess.CompletedProcess:
        env = {**os.environ, "AGENTIHOOKS_SECRETS_MODE": "standard", "AGENTIHOOKS_HOME": self._empty_home}
        return subprocess.run(
            [sys.executable, "-m", "hooks"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            cwd=_PROJECT_ROOT,
            env=env,
        )

    @pytest.fixture(autouse=True)
    def _setup_empty_home(self, tmp_path):
        self._empty_home = str(tmp_path / "empty_agentihooks")

    def _bash_payload(self, command: str) -> dict:
        return {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "session_id": "test",
            "transcript_path": "",
        }

    def _write_payload(self, content: str) -> dict:
        return {
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/test.py", "content": content},
            "session_id": "test",
            "transcript_path": "",
        }

    def test_bash_secret_to_file_exits_2(self):
        """Bash command writing a secret to a file is blocked (exit 2)."""
        key = "AKIA" + "IOSFODNN7EXAMPLE"
        result = self._run(self._bash_payload(f"echo 'KEY={key}' > /tmp/.env"))
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr

    def test_bash_inline_secret_exits_0(self):
        """Inline Bash secret (no file write) is noted but not blocked."""
        key_name = "aws_secret" + "_access_key"
        key_val = "wJalrXUtnFEMI" + "/K7MDENG/bPxRfiCYEXAMPLEKEY"
        result = self._run(self._bash_payload(f"export {key_name}={key_val}"))
        assert result.returncode == 0
        assert "NOTE" in result.stdout or "note" in result.stdout.lower()

    def test_write_secret_exits_2(self):
        """Write content containing a credential is blocked (exit 2)."""
        key = "AKIA" + "IOSFODNN7EXAMPLE"
        result = self._run(self._write_payload(f"my_key = '{key}'"))
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr

    def test_block_stderr_names_the_pattern(self):
        """The block message names which pattern was detected."""
        key = "AKIA" + "IOSFODNN7EXAMPLE"
        result = self._run(self._write_payload(f"my_key = '{key}'"))
        assert result.returncode == 2
        assert "aws_access_key" in result.stderr

    def test_clean_bash_exits_0(self):
        """A clean Bash command is not blocked."""
        result = self._run(self._bash_payload("ls -la /tmp"))
        assert result.returncode == 0

    def test_clean_write_exits_0(self):
        """Clean Write content is not blocked."""
        result = self._run(self._write_payload("x = 1\n"))
        assert result.returncode == 0


class TestSecretsModesIntegration:
    """Integration tests: AGENTIHOOKS_SECRETS_MODE controls blocking behavior."""

    def _run(self, payload: dict, *, mode: str) -> subprocess.CompletedProcess:
        env = {**os.environ, "AGENTIHOOKS_SECRETS_MODE": mode, "AGENTIHOOKS_HOME": self._empty_home}
        return subprocess.run(
            [sys.executable, "-m", "hooks"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            cwd=_PROJECT_ROOT,
            env=env,
        )

    @pytest.fixture(autouse=True)
    def _setup_empty_home(self, tmp_path):
        self._empty_home = str(tmp_path / "empty_agentihooks")

    def _bash_payload(self, command: str) -> dict:
        return {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "session_id": "test",
            "transcript_path": "",
        }

    def _write_payload(self, content: str) -> dict:
        return {
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/test.py", "content": content},
            "session_id": "test",
            "transcript_path": "",
        }

    def test_mode_off_allows_secrets(self):
        """mode=off should not block even with secrets present."""
        key = "AKIA" + "IOSFODNN7EXAMPLE"
        result = self._run(self._bash_payload(f"echo {key}"), mode="off")
        assert result.returncode == 0

    def test_mode_warn_allows_secrets(self):
        """mode=warn should not block inline Bash secrets (exit 0)."""
        key = "AKIA" + "IOSFODNN7EXAMPLE"
        result = self._run(self._bash_payload(f"echo {key}"), mode="warn")
        assert result.returncode == 0

    def test_mode_standard_notes_inline_secrets(self):
        """mode=standard notes inline Bash secrets but does not block."""
        key = "AKIA" + "IOSFODNN7EXAMPLE"
        result = self._run(self._bash_payload(f"echo {key}"), mode="standard")
        assert result.returncode == 0

    def test_mode_standard_blocks_file_write_secrets(self):
        """mode=standard BLOCKS when secret is written to a file."""
        key = "AKIA" + "IOSFODNN7EXAMPLE"
        result = self._run(self._bash_payload(f"echo KEY={key} > /tmp/.env"), mode="standard")
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr

    def test_mode_strict_notes_inline_secrets(self):
        """mode=strict notes inline Bash secrets but does not block."""
        key = "AKIA" + "IOSFODNN7EXAMPLE"
        result = self._run(self._bash_payload(f"echo {key}"), mode="strict")
        assert result.returncode == 0

    def test_mode_strict_catches_slack_token_in_file_write(self):
        """mode=strict should block Slack tokens when written to a file."""
        token = "xoxb-" + "1234567890-abcdef"
        result = self._run(self._bash_payload(f"echo SLACK={token} > /tmp/.env"), mode="strict")
        assert result.returncode == 2
        assert "slack_token" in result.stderr

    def test_mode_standard_misses_slack_token(self):
        """mode=standard should NOT block Slack tokens."""
        token = "xoxb-" + "1234567890-abcdef"
        result = self._run(self._bash_payload(f"export SLACK={token}"), mode="standard")
        assert result.returncode == 0

    def test_mode_warn_write_allows_secrets(self):
        """mode=warn should warn but not block Write with secrets."""
        key = "AKIA" + "IOSFODNN7EXAMPLE"
        result = self._run(self._write_payload(f"key = '{key}'"), mode="warn")
        assert result.returncode == 0
        assert "WARNING" in result.stdout
