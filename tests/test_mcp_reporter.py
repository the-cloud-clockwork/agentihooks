"""Tests for scripts.mcp_reporter."""

import json

import pytest

pytestmark = pytest.mark.unit


class TestMcpReporter:
    """Tests for MCP surface area reporting."""

    def test_load_all_mcp_configs_empty(self, tmp_path):
        """User-scope configs may or may not exist; just verify it returns a dict."""
        from scripts.mcp_reporter import load_all_mcp_configs

        result = load_all_mcp_configs(str(tmp_path))
        assert isinstance(result, dict)

    def test_count_tools_per_server(self):
        from scripts.mcp_reporter import count_tools_per_server

        servers = {
            "my-server": {"source": "user", "config": {"args": ["some-server"]}},
        }
        counts = count_tools_per_server(servers)
        assert "my-server" in counts
        assert counts["my-server"] > 0

    def test_count_tools_github_heuristic(self):
        from scripts.mcp_reporter import count_tools_per_server

        servers = {
            "github": {"source": "user", "config": {"args": ["github-mcp-server"]}},
        }
        counts = count_tools_per_server(servers)
        assert counts["github"] == 40

    def test_estimate_schema_tokens(self):
        from scripts.mcp_reporter import estimate_schema_tokens

        assert estimate_schema_tokens(10, 150) == 1500
        assert estimate_schema_tokens(0, 150) == 0

    def test_generate_report_empty(self):
        from scripts.mcp_reporter import generate_report

        result = generate_report({})
        assert "No MCP servers found" in result

    def test_generate_report_with_servers(self):
        from scripts.mcp_reporter import generate_report

        servers = {
            "test-server": {"source": "user", "config": {"args": ["test"]}},
        }
        result = generate_report(servers)
        assert "MCP Surface Area Report" in result
        assert "test-server" in result

    def test_generate_warning_below_threshold(self):
        """Default heuristic = 10 tools, threshold 40 → no warning."""
        from scripts.mcp_reporter import generate_warning

        servers = {
            "small": {"source": "user", "config": {"args": ["test"]}},
        }
        result = generate_warning(servers, threshold=40)
        assert result is None

    def test_generate_warning_above_threshold(self):
        """Create enough servers to exceed threshold.

        5 servers * 10 tools = 50, threshold 40 → warning.
        """
        from scripts.mcp_reporter import generate_warning

        servers = {f"server-{i}": {"source": "user", "config": {"args": ["test"]}} for i in range(5)}
        result = generate_warning(servers, threshold=40)
        assert result is not None
        assert "MCP overhead" in result

    def test_load_mcp_from_project(self, tmp_path):
        from scripts.mcp_reporter import load_all_mcp_configs

        mcp_file = tmp_path / ".mcp.json"
        mcp_file.write_text(json.dumps({"mcpServers": {"local-test": {"command": "test"}}}))

        result = load_all_mcp_configs(str(tmp_path))
        assert "local-test" in result
        assert result["local-test"]["source"] == "project"


class TestMcpReporterTargets:
    """The registry read must follow the CLI that invoked the hook."""

    def test_codex_reads_config_toml(self, tmp_path, monkeypatch):
        from scripts.mcp_reporter import load_all_mcp_configs

        (tmp_path / "config.toml").write_text('[mcp_servers.codex-only]\ncommand = "x"\n')
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")

        result = load_all_mcp_configs(str(tmp_path))
        assert list(result) == ["codex-only"]

    def test_copilot_reads_mcp_config_json(self, tmp_path, monkeypatch):
        from scripts.mcp_reporter import load_all_mcp_configs

        (tmp_path / "mcp-config.json").write_text(json.dumps({"mcpServers": {"copilot-only": {"command": "x"}}}))
        monkeypatch.setenv("COPILOT_HOME", str(tmp_path))
        monkeypatch.setenv("AGENTIHOOKS_TARGET", "copilot")

        result = load_all_mcp_configs(str(tmp_path))
        assert list(result) == ["copilot-only"]

    def test_off_target_ignores_project_mcp_json(self, tmp_path, monkeypatch):
        from scripts.mcp_reporter import load_all_mcp_configs

        (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"claude-only": {"command": "x"}}}))
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")

        assert load_all_mcp_configs(str(tmp_path)) == {}
