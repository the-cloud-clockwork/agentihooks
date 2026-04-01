"""Tests for CLAUDE.md sanity check guardrail."""

import pytest
from unittest.mock import patch

from hooks.hook_manager import BlockAction


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
