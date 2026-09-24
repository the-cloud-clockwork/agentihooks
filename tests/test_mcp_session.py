"""Tests for caller-session resolution in the agentihooks MCP tools.

The precedence here is what makes the tools correct under a shared network
server: an explicit argument is the only identity a daemon serving many
sessions from one process can trust.
"""

import pytest

from hooks.mcp._session import resolve_session_id, set_env_fallback_allowed

pytestmark = pytest.mark.unit

_ENV_VARS = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID")


@pytest.fixture(autouse=True)
def _clear_session_env(monkeypatch):
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    set_env_fallback_allowed(True)
    yield
    set_env_fallback_allowed(True)


class TestResolveSessionId:
    def test_explicit_wins_over_both_env_vars(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "from-env")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "legacy-env")

        assert resolve_session_id("explicit-id") == "explicit-id"

    def test_falls_back_to_claude_code_session_id(self, monkeypatch):
        """This is the var Claude Code actually injects into a stdio MCP subprocess."""
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "from-env")

        assert resolve_session_id() == "from-env"

    def test_claude_code_session_id_beats_legacy_name(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "real")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "legacy")

        assert resolve_session_id() == "real"

    def test_legacy_name_still_honoured_when_alone(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_SESSION_ID", "legacy")

        assert resolve_session_id() == "legacy"

    def test_returns_empty_when_nothing_resolves(self):
        assert resolve_session_id() == ""

    def test_empty_explicit_is_not_treated_as_an_answer(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "from-env")

        assert resolve_session_id("") == "from-env"


class TestEnvFallbackDisabled:
    """What a network daemon must do.

    A daemon inherits CLAUDE_CODE_SESSION_ID from whatever shell started it. If
    the env fallback stayed on, an omitted argument would not fail — it would
    resolve to that shell's session and write another agent's state. Observed
    for real before this guard existed.
    """

    def test_env_is_ignored_when_fallback_disabled(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "the-launching-shells-session")
        set_env_fallback_allowed(False)

        assert resolve_session_id() == ""

    def test_explicit_still_resolves_when_fallback_disabled(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "the-launching-shells-session")
        set_env_fallback_allowed(False)

        assert resolve_session_id("real-caller") == "real-caller"


class TestBuildServerSetsFallbackPolicy:
    """build_server decides the policy once, so tool bodies never branch."""

    def test_stdio_keeps_env_fallback(self, monkeypatch):
        from hooks.mcp import build_server

        monkeypatch.delenv("MCP_TRANSPORT", raising=False)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "stdio-session")
        build_server()

        assert resolve_session_id() == "stdio-session"

    @pytest.mark.parametrize("transport", ["sse", "streamable-http"])
    def test_network_transports_disable_env_fallback(self, monkeypatch, transport):
        from hooks.mcp import build_server

        monkeypatch.setenv("MCP_TRANSPORT", transport)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "inherited-from-shell")
        build_server()

        assert resolve_session_id() == ""
