"""Tests for hooks/mcp/utilities.py — write_markdown, get_env, hooks_list_tools."""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class _MockMCP:
    """Minimal MCP stub that captures registered tools by name."""

    def __init__(self):
        self.tools: dict = {}
        self._tool_manager = MagicMock()
        self._tool_manager.list_tools.return_value = []

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


@pytest.fixture(scope="module")
def mcp_tools():
    from hooks.mcp import utilities

    mcp = _MockMCP()
    utilities.register(mcp)
    return mcp.tools


# ---------------------------------------------------------------------------
# write_markdown
# ---------------------------------------------------------------------------


class TestWriteMarkdown:
    def test_rejects_non_md_extension(self, mcp_tools):
        fn = mcp_tools["write_markdown"]
        result = json.loads(fn(filepath="/tmp/file.txt", content="hello"))
        assert result["success"] is False
        assert ".md" in result["error"]

    def test_rejects_disallowed_path(self, mcp_tools):
        fn = mcp_tools["write_markdown"]
        result = json.loads(fn(filepath="/etc/forbidden.md", content="hello"))
        assert result["success"] is False
        assert "not allowed" in result["error"].lower()

    def test_writes_to_tmp(self, mcp_tools, tmp_path):
        md = Path("/tmp") / f"agentihooks_test_{os.getpid()}.md"
        fn = mcp_tools["write_markdown"]
        try:
            result = json.loads(fn(filepath=str(md), content="# Hello\n"))
            assert result["success"] is True
            assert result["bytes_written"] > 0
            assert md.exists()
        finally:
            md.unlink(missing_ok=True)

    def test_exception_returns_error(self, mcp_tools):
        fn = mcp_tools["write_markdown"]
        with patch("pathlib.Path.write_text", side_effect=OSError("disk full")):
            result = json.loads(fn(filepath="/tmp/fail_test.md", content="hi"))
        assert result["success"] is False


# ---------------------------------------------------------------------------
# get_env
# ---------------------------------------------------------------------------


class TestGetEnv:
    def test_rejects_empty_filter(self, mcp_tools):
        fn = mcp_tools["get_env"]
        result = json.loads(fn())
        assert result["success"] is False
        assert "filter" in result["error"].lower()

    def test_filters_by_name(self, mcp_tools):
        fn = mcp_tools["get_env"]
        with patch.dict(os.environ, {"MY_TEST_VAR_XYZ": "testval"}):
            result = json.loads(fn(filter="MY_TEST_VAR_XYZ"))
        assert result["success"] is True
        assert "MY_TEST_VAR_XYZ" in result["variables"]
        assert result["count"] == 1

    def test_case_insensitive_filter(self, mcp_tools):
        fn = mcp_tools["get_env"]
        with patch.dict(os.environ, {"AGENTIHOOKS_TEST_FILTER": "val"}):
            result = json.loads(fn(filter="agentihooks_test_filter"))
        assert result["success"] is True
        assert result["count"] >= 1

    def test_no_match_returns_empty(self, mcp_tools):
        fn = mcp_tools["get_env"]
        result = json.loads(fn(filter="ZZZZ_NONEXISTENT_ZZZZ_12345"))
        assert result["success"] is True
        assert result["count"] == 0

    def test_exception_returns_error(self, mcp_tools):
        fn = mcp_tools["get_env"]
        with patch("builtins.dict", side_effect=Exception("boom")):
            result = json.loads(fn())
        assert result["success"] is False


# ---------------------------------------------------------------------------
# hooks_list_tools
# ---------------------------------------------------------------------------


class TestHooksListTools:
    def test_returns_tool_list(self):
        from hooks.mcp import utilities

        mcp = _MockMCP()
        utilities.register(mcp)
        fn = mcp.tools["hooks_list_tools"]
        result = json.loads(fn())
        assert result["success"] is True
        assert "total_tools" in result
        assert "categories" in result
        assert "available_categories" in result

    def test_filters_to_active_tools(self):
        from hooks.mcp import utilities

        mcp = _MockMCP()
        mcp._tool_manager.list_tools.return_value = []
        utilities.register(mcp)
        fn = mcp.tools["hooks_list_tools"]
        result = json.loads(fn())
        assert result["success"] is True
        assert result["total_tools"] == 0
