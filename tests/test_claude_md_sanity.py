"""Tests for CLAUDE.md sanity check guardrail."""

import json
import os
import subprocess
import sys
from unittest.mock import patch

import pytest

from hooks.hook_manager import BlockAction

_MANY_HOOKS = """
import io, json, os, sys
from hooks.hook_manager import main

class Exit(BaseException):
    pass

def fake_exit(code):
    raise Exit(code)

os._exit = fake_exit
out, results = sys.stdout, []
for payload in json.load(sys.stdin):
    sys.stdin, sys.stdout, sys.stderr = io.StringIO(json.dumps(payload)), io.StringIO(), io.StringIO()
    try:
        main()
    except Exit as done:
        results.append({"returncode": done.args[0], "stdout": sys.stdout.getvalue(), "stderr": sys.stderr.getvalue()})
out.write(json.dumps(results))
"""


@pytest.fixture(autouse=True)
def _disable_redis():
    with patch("hooks._redis.get_redis", return_value=None):
        yield


class TestClaudeMdSanity:
    """Tests for hooks.context.claude_md_sanity.check_claude_md_write."""

    def test_skip_non_claude_md(self):
        from hooks.context.claude_md_sanity import check_claude_md_write

        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/README.md", "content": "x\n" * 999},
        }
        check_claude_md_write(payload)  # should not raise

    def test_write_under_limit(self):
        from hooks.context.claude_md_sanity import check_claude_md_write

        with patch("hooks.context.claude_md_sanity.CLAUDE_MD_MAXLINES", 400):
            payload = {
                "tool_name": "Write",
                "tool_input": {"file_path": "/tmp/CLAUDE.md", "content": "line\n" * 100},
            }
            check_claude_md_write(payload)

    def test_write_over_limit_blocked(self):
        from hooks.context.claude_md_sanity import check_claude_md_write

        with patch("hooks.context.claude_md_sanity.CLAUDE_MD_MAXLINES", 200):
            payload = {
                "tool_name": "Write",
                "tool_input": {"file_path": "/tmp/CLAUDE.md", "content": "line\n" * 500},
            }
            with pytest.raises(BlockAction, match="500 lines"):
                check_claude_md_write(payload)

    def test_edit_over_limit_blocked(self, tmp_path):
        from hooks.context.claude_md_sanity import check_claude_md_write

        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("line\n" * 190)

        with patch("hooks.context.claude_md_sanity.CLAUDE_MD_MAXLINES", 200):
            payload = {
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": str(claude_md),
                    "old_string": "line\n",
                    "new_string": "line\n" * 50,
                },
            }
            with pytest.raises(BlockAction):
                check_claude_md_write(payload)

    def test_edit_under_limit(self, tmp_path):
        from hooks.context.claude_md_sanity import check_claude_md_write

        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("old\nline\n" * 10)

        with patch("hooks.context.claude_md_sanity.CLAUDE_MD_MAXLINES", 400):
            payload = {
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": str(claude_md),
                    "old_string": "old\n",
                    "new_string": "new\n",
                },
            }
            check_claude_md_write(payload)

    def test_nested_claude_md_path(self):
        from hooks.context.claude_md_sanity import check_claude_md_write

        with patch("hooks.context.claude_md_sanity.CLAUDE_MD_MAXLINES", 10):
            payload = {
                "tool_name": "Write",
                "tool_input": {
                    "file_path": "/home/user/project/.claude/CLAUDE.md",
                    "content": "x\n" * 50,
                },
            }
            with pytest.raises(BlockAction):
                check_claude_md_write(payload)

    def test_message_signal_raises_session_limit(self, tmp_path):
        from hooks.context import claude_md_sanity

        with (
            patch.object(claude_md_sanity, "_LIMIT_DIR", tmp_path),
            patch.object(claude_md_sanity, "CLAUDE_MD_MAXLINES", 200),
        ):
            requested = claude_md_sanity.parse_max_lines_signal(
                "Update the project claude-md-max-lines=600 and continue"
            )
            assert requested == 600
            assert claude_md_sanity.set_session_max_lines("session-1", requested) == 600

            payload = {
                "session_id": "session-1",
                "tool_name": "Write",
                "tool_input": {"file_path": "/tmp/CLAUDE.md", "content": "line\n" * 600},
            }
            claude_md_sanity.check_claude_md_write(payload)

            payload["tool_input"]["content"] += "line\n"
            with pytest.raises(BlockAction, match="cap of 600 lines"):
                claude_md_sanity.check_claude_md_write(payload)

    def test_signal_only_raises_baseline(self, tmp_path):
        from hooks.context import claude_md_sanity

        with (
            patch.object(claude_md_sanity, "_LIMIT_DIR", tmp_path),
            patch.object(claude_md_sanity, "CLAUDE_MD_MAXLINES", 200),
        ):
            assert claude_md_sanity.set_session_max_lines("session-1", 100) is None
            assert claude_md_sanity.get_max_lines("session-1") == 200

    def test_last_message_signal_wins_and_session_end_clears_it(self, tmp_path):
        from hooks.context import claude_md_sanity

        with (
            patch.object(claude_md_sanity, "_LIMIT_DIR", tmp_path),
            patch.object(claude_md_sanity, "CLAUDE_MD_MAXLINES", 200),
        ):
            requested = claude_md_sanity.parse_max_lines_signal("claude-md-max-lines=400 then claude-md-max-lines=750")
            assert requested == 750
            claude_md_sanity.set_session_max_lines("session-1", requested)
            assert claude_md_sanity.get_max_lines("session-1") == 750
            claude_md_sanity.clear_session_max_lines("session-1")
            assert claude_md_sanity.get_max_lines("session-1") == 200

    @pytest.mark.parametrize(
        "text",
        [
            "claude-md-max-lines=0",
            "claude-md-max-lines=-1",
            "xclaude-md-max-lines=600",
            "claude-md-max-lines=600x",
        ],
    )
    def test_invalid_message_signals_are_ignored(self, text):
        from hooks.context.claude_md_sanity import parse_max_lines_signal

        assert parse_max_lines_signal(text) is None

    def test_message_signal_crosses_hook_processes_and_clears_on_session_end(self, tmp_path):
        env = {
            **os.environ,
            "AGENTIHOOKS_HOME": str(tmp_path),
            "HOME": str(tmp_path),
            "REDIS_URL": "",
            "AGENTIHOOKS_DISABLE_BYPASS_LOOKUP": "1",
            "AGENTIHOOKS_SECRETS_MODE": "off",
        }
        session_id = "claude-md-cap-integration"
        write = {
            "hook_event_name": "PreToolUse",
            "session_id": session_id,
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/CLAUDE.md", "content": "line\n" * 600},
        }
        over = {**write, "tool_input": {**write["tool_input"], "content": "line\n" * 601}}
        reset = {**write, "tool_input": {**write["tool_input"], "content": "line\n" * 201}}
        payloads = [
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": session_id,
                "prompt": "Continue with claude-md-max-lines=600",
            },
            write,
            over,
            {"hook_event_name": "SessionEnd", "session_id": session_id},
            reset,
        ]
        proc = subprocess.run(
            [sys.executable, "-c", _MANY_HOOKS],
            input=json.dumps(payloads),
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        prompt, allowed, blocked, ended, after_end = json.loads(proc.stdout)

        assert prompt["returncode"] == 0
        assert "CLAUDE.md line cap raised to 600 for this session." in prompt["stdout"]
        assert allowed["returncode"] == 0
        assert blocked["returncode"] == 2
        assert "cap of 600 lines" in blocked["stderr"]
        assert ended["returncode"] == 0
        assert after_end["returncode"] == 2
        assert "cap of 200 lines" in after_end["stderr"]
