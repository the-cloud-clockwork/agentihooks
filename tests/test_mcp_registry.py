"""Tests for hooks.mcp module."""

import json

import pytest

pytestmark = pytest.mark.unit

_SESSION_ENV_VARS = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID")


@pytest.fixture
def tools(monkeypatch):
    """A freshly built server's raw tool callables, keyed by tool name.

    The session-id env vars are cleared so a test that does not set them
    exercises the "nothing resolves" path rather than picking up the developer's
    own session.
    """
    for var in _SESSION_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MCP_CATEGORIES", "all")

    from hooks.mcp import build_server

    server = build_server()
    return {name: tool.fn for name, tool in server._tool_manager._tools.items()}


class TestMCPRegistry:
    """Test MCP tool registry and server building."""

    def test_category_modules_defined(self):
        """CATEGORY_MODULES maps categories to module paths."""
        from hooks.mcp._registry import CATEGORY_MODULES

        assert isinstance(CATEGORY_MODULES, dict)
        assert len(CATEGORY_MODULES) > 0

    def test_known_categories(self):
        """Only the agentihooks-native categories are registered."""
        from hooks.mcp._registry import CATEGORY_MODULES

        assert set(CATEGORY_MODULES) == {"channels", "enforcement", "conditions"}

    def test_build_server_callable(self):
        """build_server() is importable and callable."""
        from hooks.mcp import build_server

        assert callable(build_server)


class TestTransportResolution:
    """MCP_TRANSPORT selects the transport; anything unrecognised degrades to stdio."""

    def test_defaults_to_stdio_when_unset(self, monkeypatch):
        from hooks.mcp import resolve_transport

        monkeypatch.delenv("MCP_TRANSPORT", raising=False)
        assert resolve_transport() == "stdio"

    @pytest.mark.parametrize("value", ["sse", "streamable-http", "STDIO", " sse "])
    def test_accepts_valid_transports(self, monkeypatch, value):
        from hooks.mcp import resolve_transport

        monkeypatch.setenv("MCP_TRANSPORT", value)
        assert resolve_transport() == value.strip().lower()

    def test_unknown_value_warns_and_falls_back(self, monkeypatch, capsys):
        """A typo must not take the whole toolbelt offline."""
        from hooks.mcp import resolve_transport

        monkeypatch.setenv("MCP_TRANSPORT", "websocket")
        assert resolve_transport() == "stdio"
        assert "websocket" in capsys.readouterr().err


class TestServerNetworkSettings:
    """Host/port/paths are FastMCP constructor settings, not .run() arguments."""

    def test_defaults(self, monkeypatch):
        from hooks.mcp import build_server

        for var in ("MCP_HOST", "MCP_PORT", "MCP_SSE_PATH", "MCP_STREAMABLE_HTTP_PATH", "MCP_STATELESS_HTTP"):
            monkeypatch.delenv(var, raising=False)

        settings = build_server().settings
        # Must match the installer's url default, not merely be loopback: a bind
        # on 127.0.0.1 under a url naming `localhost` breaks wherever that name
        # resolves to ::1 first.
        assert settings.host == "localhost"
        assert settings.port == 8642
        assert settings.sse_path == "/sse"
        assert settings.streamable_http_path == "/mcp"
        assert settings.stateless_http is False

    def test_env_overrides(self, monkeypatch):
        from hooks.mcp import build_server

        monkeypatch.setenv("MCP_HOST", "0.0.0.0")
        monkeypatch.setenv("MCP_PORT", "9001")
        monkeypatch.setenv("MCP_SSE_PATH", "/events")
        monkeypatch.setenv("MCP_STREAMABLE_HTTP_PATH", "/rpc")
        monkeypatch.setenv("MCP_STATELESS_HTTP", "true")

        settings = build_server().settings
        assert settings.host == "0.0.0.0"
        assert settings.port == 9001
        assert settings.sse_path == "/events"
        assert settings.streamable_http_path == "/rpc"
        assert settings.stateless_http is True

    def test_non_integer_port_warns_and_uses_default(self, monkeypatch, capsys):
        from hooks.mcp import build_server

        monkeypatch.setenv("MCP_PORT", "not-a-port")
        settings = build_server().settings
        assert settings.port == 8642
        assert "MCP_PORT" in capsys.readouterr().err


class TestSessionScopedTools:
    """The tool that needs to know who is calling.

    Under a network transport one process serves every session, so an explicit
    argument is the only trustworthy identity — these pin that it is honoured
    and that an unresolvable caller fails loudly instead of silently writing
    state against the wrong session.
    """

    SESSION_SCOPED = ("channel_acknowledge",)

    def test_all_session_scoped_tools_accept_session_id(self, tools):
        import inspect

        for name in self.SESSION_SCOPED:
            assert "session_id" in inspect.signature(tools[name]).parameters, name

    def test_channel_acknowledge_rejects_unresolvable_caller(self, tools, monkeypatch):
        """Regression: this used to ack against the literal "unknown", so the
        broadcast kept re-injecting for every real session forever.

        Same reasoning as above — acknowledge_broadcast("") would fail anyway
        with "message not found", masking a deleted guard.
        """

        def _explode(*args, **kwargs):
            raise AssertionError("acknowledge_broadcast must not be reached without a caller")

        monkeypatch.setattr("hooks.context.broadcast.acknowledge_broadcast", _explode)
        result = json.loads(tools["channel_acknowledge"]("some-message-id"))

        assert result["success"] is False
        assert "session_id" in result["error"]

    def test_channel_acknowledge_uses_explicit_session_id(self, tools, monkeypatch):
        seen = {}

        def _fake_acknowledge(session_id, message_id):
            seen["session_id"] = session_id
            return True

        monkeypatch.setattr("hooks.context.broadcast.acknowledge_broadcast", _fake_acknowledge)
        result = json.loads(tools["channel_acknowledge"]("some-message-id", session_id="explicit-sid"))

        assert result["success"] is True
        assert seen["session_id"] == "explicit-sid"

    def test_explicit_session_id_beats_env(self, tools, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "env-sid")
        seen = {}

        def _fake_acknowledge(session_id, message_id):
            seen["session_id"] = session_id
            return True

        monkeypatch.setattr("hooks.context.broadcast.acknowledge_broadcast", _fake_acknowledge)
        tools["channel_acknowledge"]("some-message-id", session_id="explicit-sid")

        assert seen["session_id"] == "explicit-sid"

    def test_env_fallback_still_works_under_stdio(self, tools, monkeypatch):
        """Omitting the argument must keep working for one-process-per-session."""
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "env-sid")
        seen = {}

        def _fake_acknowledge(session_id, message_id):
            seen["session_id"] = session_id
            return True

        monkeypatch.setattr("hooks.context.broadcast.acknowledge_broadcast", _fake_acknowledge)
        tools["channel_acknowledge"]("some-message-id")

        assert seen["session_id"] == "env-sid"
