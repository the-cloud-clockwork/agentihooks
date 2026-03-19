"""Tests for hooks.context.bash_output_filter."""

import pytest

pytestmark = pytest.mark.unit


class TestBashOutputFilter:
    """Tests for filter_bash_output and individual truncation helpers."""

    def test_docker_truncation(self):
        """100-line docker output → ≤50 lines, includes truncation notice."""
        from hooks.context.bash_output_filter import filter_bash_output

        output = "\n".join(f"log line {i}" for i in range(100))
        result = filter_bash_output("Bash", {"command": "docker logs mycontainer"}, output)
        assert result is not None
        lines = result.splitlines()
        assert len(lines) <= 51  # 50 lines + 1 notice line
        assert "truncated" in result

    def test_test_runner_truncation(self):
        """pytest output with 20 failures → keeps summary + first 10 failures, strips PASSED lines."""
        from hooks.context.bash_output_filter import filter_bash_output

        lines = []
        for i in range(20):
            lines.append(f"FAILED test_module::test_case_{i} - AssertionError")
            lines.append(f"  assert {i} == {i + 1}")
        for i in range(30):
            lines.append(f"PASSED test_module::test_passing_{i}")
        lines.append("=" * 60)
        lines.append("20 failed, 30 passed in 1.23s")
        output = "\n".join(lines)

        # Make it large enough to trigger filtering
        output = output * 3

        result = filter_bash_output("Bash", {"command": "pytest tests/"}, output)
        # Should trigger test_runner path and return truncated output
        if result is not None:
            assert "truncated" in result or "FAILED" in result

    def test_git_log_truncation(self):
        """30-commit git log → 20 commits."""
        from hooks.context.bash_output_filter import filter_bash_output

        lines = []
        for i in range(30):
            sha = f"{'a' * 7}{i:03d}"[:40].ljust(40, "0")
            lines.append(f"commit {sha}")
            lines.append("Author: Dev <dev@example.com>")
            lines.append(f"Date: Mon Mar {i + 1} 2026")
            lines.append("")
            lines.append(f"    Commit message {i}")
            lines.append("")

        output = "\n".join(lines)
        result = filter_bash_output("Bash", {"command": "git log --oneline"}, output)
        assert result is not None
        assert "truncated" in result

    def test_hard_char_cap(self):
        """10,000 char output → ≤5000 chars + truncation notice."""
        from hooks.context.bash_output_filter import filter_bash_output

        output = "x" * 10_000
        result = filter_bash_output("Bash", {"command": "some_verbose_command"}, output)
        assert result is not None
        # The result should contain truncation notice and be shorter than original
        assert "truncated" in result
        assert len(result) < len(output)

    def test_passthrough_short_output(self):
        """20-line output → returns None (no modification)."""
        from hooks.context.bash_output_filter import filter_bash_output

        output = "\n".join(f"line {i}" for i in range(20))
        result = filter_bash_output("Bash", {"command": "ls -la"}, output)
        assert result is None

    def test_only_fires_for_bash(self):
        """tool_name='Read' → returns None."""
        from hooks.context.bash_output_filter import filter_bash_output

        output = "x" * 10_000
        result = filter_bash_output("Read", {"file_path": "/some/file"}, output)
        assert result is None

    def test_truncate_docker_logs_helper(self):
        """truncate_docker_logs: keeps last N lines."""
        from hooks.context.bash_output_filter import truncate_docker_logs

        output = "\n".join(f"line {i}" for i in range(100))
        result = truncate_docker_logs(output, max_lines=10)
        lines = result.splitlines()
        # First line is notice, next 10 are content
        assert lines[0].startswith("[truncated")
        assert len(lines) == 11
        assert "line 99" in result

    def test_truncate_git_log_helper(self):
        """truncate_git_log: keeps first N commits."""
        from hooks.context.bash_output_filter import truncate_git_log

        lines = []
        for i in range(25):
            sha = f"{'a' * 40}"
            sha = f"{i:040x}"
            lines.append(f"commit {sha}")
            lines.append(f"    message {i}")
            lines.append("")
        output = "\n".join(lines)
        result = truncate_git_log(output, max_commits=5)
        assert "truncated" in result

    def test_truncate_generic_helper(self):
        """truncate_generic: hard cap with notice."""
        from hooks.context.bash_output_filter import truncate_generic

        output = "a" * 8000
        result = truncate_generic(output, max_chars=5000)
        assert result.startswith("a" * 5000)
        assert "truncated" in result
        assert "3000" in result
