"""Tests for the tool matcher grammar shared by conditions and enforcements."""

import pytest

from hooks.context.tool_matcher import command_heads, parse


class TestParse:
    @pytest.mark.parametrize(
        "token, kinds",
        [
            ("any", ["any"]),
            ("Bash", ["tool"]),
            ("mcp", ["mcp"]),
            ("mcp__gateway-tools", ["mcp_server"]),
            ("mcp__gateway-tools__github-create_pull_request", ["tool"]),
            ("bash.git", ["cli"]),
            ("edit+write", ["tool", "tool"]),
            ("bash.git+mcp", ["cli", "mcp"]),
        ],
    )
    def test_valid(self, token, kinds):
        assert [alt.kind for alt in parse(token).alternatives] == kinds

    def test_token_is_normalized(self):
        assert parse("  Edit+WRITE ").token == "edit+write"

    @pytest.mark.parametrize("token", ["", "  ", "a++b", "+bash", "mcp__", "mcp__srv__", "bash.", "a b", "a*", "a|b"])
    def test_invalid(self, token):
        with pytest.raises(ValueError):
            parse(token)


class TestMatches:
    def test_exact_is_case_insensitive(self):
        assert parse("bash").matches("Bash")
        assert not parse("bash").matches("Read")

    def test_any(self):
        assert parse("any").matches("WebFetch")

    def test_mcp_scopes(self):
        name = "mcp__gateway-tools__github-create_pull_request"
        assert parse("mcp").matches(name)
        assert parse("mcp__gateway-tools").matches(name)
        assert not parse("mcp__gateway").matches(name)
        assert parse(name).matches(name)
        assert not parse("mcp").matches("Bash")

    def test_cli_matches_any_simple_command(self):
        matcher = parse("bash.git")
        assert matcher.matches("Bash", {"command": "cd repo && git push"})
        assert not matcher.matches("Bash", {"command": "echo git"})
        assert not matcher.matches("Read", {"command": "git status"})

    def test_alternation(self):
        matcher = parse("edit+write")
        assert matcher.matches("Write") and matcher.matches("Edit")
        assert not matcher.matches("NotebookEdit")


class TestCommandHeads:
    @pytest.mark.parametrize(
        "command, heads",
        [
            ("cd x && git push origin main", {"cd", "git"}),
            ('grep "a; rm x" f | wc -l', {"grep", "wc"}),
            ("(cd x; git push)", {"cd", "git"}),
            ("ls\nrm -rf y", {"ls", "rm"}),
            ("echo hi > out 2>&1; kubectl get po", {"echo", "kubectl"}),
            ("FOO=1 sudo -u bob env BAR=2 git status", {"git"}),
            ("timeout 30 /usr/bin/python3 -m x", {"python3"}),
            ("cat <<EOF\nrm -rf /\nEOF\nls", {"cat", "ls"}),
            ("if git diff; then echo ok; fi", {"git", "echo", "fi"}),
            ("echo 'unterminated", {"echo"}),
            ("", set()),
        ],
    )
    def test_heads(self, command, heads):
        assert command_heads(command) == heads
