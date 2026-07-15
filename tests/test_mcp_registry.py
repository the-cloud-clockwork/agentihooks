"""Tests for hooks.mcp module."""

import pytest

pytestmark = pytest.mark.unit


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

        assert set(CATEGORY_MODULES) == {"channels", "enforcement"}

    def test_build_server_callable(self):
        """build_server() is importable and callable."""
        from hooks.mcp import build_server

        assert callable(build_server)
