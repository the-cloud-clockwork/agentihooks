"""Tests for hooks.hook_manager module."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_PROJECT_ROOT = Path(__file__).parent.parent


class TestSessionIdBanner:
    """SessionStart tells the agent its own session id.

    That id is the argument the session-scoped agentihooks tools take, and it is
    the only identity that works when agentihooks runs as one network server
    shared by every session — so the agent has to be told it.
    """

    @pytest.fixture(autouse=True)
    def _setup_empty_home(self, tmp_path):
        self._empty_home = str(tmp_path / "empty_agentihooks")

    def _run(self, *, enabled: str) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "AGENTIHOOKS_HOME": self._empty_home,
            "AGENTIHOOKS_DISABLE_BYPASS_LOOKUP": "1",
            "MCP_SESSION_ID_BANNER_ENABLED": enabled,
        }
        return subprocess.run(
            [sys.executable, "-m", "hooks"],
            input=json.dumps(
                {"hook_event_name": "SessionStart", "session_id": "sid-abc-123", "cwd": str(_PROJECT_ROOT)}
            ),
            capture_output=True,
            text=True,
            cwd=_PROJECT_ROOT,
            env=env,
        )

    def test_banner_names_the_session_id(self):
        result = self._run(enabled="true")

        assert "sid-abc-123" in result.stdout
        assert "session_id" in result.stdout

    def test_banner_suppressed_when_disabled(self):
        result = self._run(enabled="false")

        assert "sid-abc-123" not in result.stdout


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


@pytest.fixture
def brain_dispatch(monkeypatch):
    from unittest.mock import Mock

    from hooks import config, hook_manager

    for flag in ("MEMORY_AUTO_SAVE", "CONTEXT_AUDIT_ENABLED", "VOICE_ENABLED"):
        monkeypatch.setattr(config, flag, False)
    monkeypatch.setattr(hook_manager, "_request_trace_flush", Mock())
    monkeypatch.setattr(hook_manager, "parse_transcript_metrics", Mock(return_value={}))
    monkeypatch.setattr("hooks.tool_memory.scan_transcript", Mock())
    monkeypatch.setattr("hooks.lifecycle.refresh.on_stop", Mock(return_value=False))
    monkeypatch.setattr("hooks.lifecycle.handoff_close.on_stop", Mock(return_value=False))
    monkeypatch.setattr(hook_manager.otel, "get_tracer", Mock(return_value=None))
    fork = Mock()
    monkeypatch.setattr("hooks._async.fork_and_call", fork)
    return fork


@pytest.mark.parametrize("event, task_name", [("Stop", "brain_writer"), ("SubagentStop", "brain_writer_subagent")])
@pytest.mark.parametrize(
    "transcript_path, last_message", [("transcript.jsonl", ""), ("", "marker"), ("transcript.jsonl", "marker")]
)
def test_brain_writer_dispatch(event, task_name, transcript_path, last_message, brain_dispatch, monkeypatch):
    from unittest.mock import call

    from hooks.context.brain_writer_hook import write_markers
    from hooks.hook_manager import EVENT_HANDLERS

    monkeypatch.setattr("hooks.config.BRAIN_WRITER_ENABLED", True)
    payload = {
        "session_id": "session",
        "transcript_path": transcript_path,
        "last_assistant_message": last_message,
    }
    if event == "SubagentStop":
        payload.update(agent_id="agent", agent_transcript_path=transcript_path)
    EVENT_HANDLERS[event](payload)

    assert [c for c in brain_dispatch.call_args_list if c.args[0] is write_markers] == [
        call(
            write_markers,
            "agent" if event == "SubagentStop" else "session",
            transcript_path,
            last_message=last_message,
            timeout_sec=60,
            task_name=task_name,
        )
    ]


@pytest.mark.parametrize("event", ["Stop", "SubagentStop"])
@pytest.mark.parametrize(
    "enabled, transcript_path, last_message", [(False, "transcript.jsonl", "marker"), (True, "", "")]
)
def test_brain_writer_skips_dispatch(event, enabled, transcript_path, last_message, brain_dispatch, monkeypatch):
    from hooks.context.brain_writer_hook import write_markers
    from hooks.hook_manager import EVENT_HANDLERS

    monkeypatch.setattr("hooks.config.BRAIN_WRITER_ENABLED", enabled)
    EVENT_HANDLERS[event](
        {"session_id": "session", "transcript_path": transcript_path, "last_assistant_message": last_message}
    )

    assert not any(c.args[0] is write_markers for c in brain_dispatch.call_args_list)


@pytest.mark.parametrize("enabled", [True, False])
def test_brain_reader_session_start_dispatch(enabled, brain_dispatch, monkeypatch, tmp_path):
    from unittest.mock import Mock

    from hooks import config
    from hooks.hook_manager import EVENT_HANDLERS

    monkeypatch.setattr(config, "BRAIN_ENABLED", enabled)
    for flag in (
        "PROJECT_BRIDGE_ENABLED",
        "MCP_SESSION_ID_BANNER_ENABLED",
        "MCP_HYGIENE_ENABLED",
        "EFFORT_POLICY_ENABLED",
        "AGENTIHOOKS_FORCE_DEV_BRANCH",
        "CI_MANIFESTO_ENABLED",
        "BROADCAST_ENABLED",
    ):
        monkeypatch.setattr(config, flag, False)
    monkeypatch.setattr("hooks.lifecycle.guard.session_event", Mock())
    monkeypatch.setattr("hooks.lifecycle.deps_kick.kick", Mock())
    monkeypatch.setattr("hooks.context.injection_trace.record_session_start", Mock())
    monkeypatch.setattr("hooks.context.enforcement.get_session_start_enforcements", Mock(return_value=""))
    monkeypatch.setattr("hooks.context.voice_output.cleanup_stale_flags", Mock(return_value=0))
    inject = Mock()
    monkeypatch.setattr("hooks.context.brain_adapter.inject_on_session_start", inject)
    EVENT_HANDLERS["SessionStart"]({"session_id": "session", "cwd": str(tmp_path)})

    if enabled:
        inject.assert_called_once_with("session", str(tmp_path))
    else:
        inject.assert_not_called()


@pytest.mark.parametrize(
    "enabled, count, claim", [(True, 7, True), (True, 7, False), (True, 0, True), (False, 7, True)]
)
def test_brain_reader_pretool_dispatch(enabled, count, claim, monkeypatch):
    from unittest.mock import Mock

    from hooks import config, hook_manager
    from hooks.targets import emitter

    monkeypatch.setattr(config, "BRAIN_ENABLED", enabled)
    monkeypatch.setattr(config, "SECRETS_MODE", "off")
    for flag in (
        "QUOTA_POLICY_ENABLED",
        "QUOTA_USAGE_INJECTION_ENABLED",
        "BROADCAST_ENABLED",
        "ENFORCEMENT_INJECTION_ENABLED",
        "RETRY_BREAKER_ENABLED",
    ):
        monkeypatch.setattr(config, flag, False)
    monkeypatch.setattr("hooks.context.enforcement.increment_and_get_count", Mock(return_value=count))
    monkeypatch.setattr("hooks.targets.capabilities.can_inject_context", Mock(return_value=claim))
    monkeypatch.setattr("hooks.context.context_recycle.directive", Mock(return_value=None))
    monkeypatch.setattr("hooks.lifecycle.guard.pretool", Mock(return_value=None))
    monkeypatch.setattr("hooks.tool_memory.inject_memory", Mock())
    for name in ("_inbox_blocks", "_refocus_blocks", "_wait_nudge_blocks"):
        monkeypatch.setattr(hook_manager, name, Mock(return_value=[]))
    refresh = Mock(return_value="brain refresh")
    monkeypatch.setattr("hooks.context.brain_adapter.maybe_refresh_on_tool_call", refresh)
    buffer = Mock()
    monkeypatch.setattr(emitter, "buffer_context", buffer)

    hook_manager.on_pre_tool_use({"session_id": "session", "tool_name": "Unknown", "tool_input": {}})

    if enabled and count:
        refresh.assert_called_once_with("session", count, claim_delivery=claim)
    else:
        refresh.assert_not_called()
    if enabled and count and claim:
        buffer.assert_called_once_with("brain refresh")
    else:
        buffer.assert_not_called()


class TestBlockActionIntegration:
    """Integration tests: BlockAction propagates through main() with exit 2."""

    def _run(self, payload: dict) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "AGENTIHOOKS_SECRETS_MODE": "standard",
            "AGENTIHOOKS_HOME": self._empty_home,
            "AGENTIHOOKS_DISABLE_BYPASS_LOOKUP": "1",
        }
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

    def _mcp_payload(self, tool_name: str, tool_input: dict) -> dict:
        return {
            "hook_event_name": "PreToolUse",
            "tool_name": tool_name,
            "tool_input": tool_input,
            "session_id": "test",
            "transcript_path": "",
        }

    def test_mcp_symbol_edit_secret_exits_2(self):
        """A Serena symbol edit carrying a credential is blocked (exit 2)."""
        key = "AKIA" + "IOSFODNN7EXAMPLE"
        result = self._run(
            self._mcp_payload(
                "mcp__serena__replace_symbol_body",
                {"name_path": "Config", "relative_path": "app.py", "body": f"KEY = '{key}'"},
            )
        )
        assert result.returncode == 2
        assert "aws_access_key" in result.stderr

    def test_mcp_nested_secret_exits_2(self):
        """A credential nested in a list of file objects is blocked (exit 2)."""
        key = "AKIA" + "IOSFODNN7EXAMPLE"
        result = self._run(
            self._mcp_payload(
                "mcp__github__push_files",
                {"branch": "dev", "files": [{"path": "a.py", "content": "x = 1"}, {"path": "b.py", "content": key}]},
            )
        )
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr

    def test_clean_mcp_exits_0(self):
        """A clean MCP call is not blocked."""
        result = self._run(self._mcp_payload("mcp__serena__find_symbol", {"name_path_pattern": "Config"}))
        assert result.returncode == 0


class TestSecretsModesIntegration:
    """Integration tests: AGENTIHOOKS_SECRETS_MODE controls blocking behavior."""

    def _run(self, payload: dict, *, mode: str) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "AGENTIHOOKS_SECRETS_MODE": mode,
            "AGENTIHOOKS_HOME": self._empty_home,
            "AGENTIHOOKS_DISABLE_BYPASS_LOOKUP": "1",
        }
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

    def test_mode_warn_mcp_allows_secrets(self):
        """mode=warn should warn but not block an MCP call with secrets."""
        key = "AKIA" + "IOSFODNN7EXAMPLE"
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "mcp__serena__replace_content",
            "tool_input": {"relative_path": "app.py", "needle": "x", "repl": key, "mode": "literal"},
            "session_id": "test",
            "transcript_path": "",
        }
        result = self._run(payload, mode="warn")
        assert result.returncode == 0
        assert "WARNING" in result.stdout
