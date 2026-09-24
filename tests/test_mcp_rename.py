"""The MCP server is registered as `agentihooks`; `agentihooks init` replaces the
legacy `hooks-utils` entry it owns on every target, and leaves a foreign one alone."""

import json

import install
import pytest

from scripts.targets._common import LEGACY_MCP_SERVER_NAMES, MCP_SERVER_NAME
from scripts.targets.codex_target import CodexAdapter, codex_home
from scripts.targets.copilot_target import CopilotAdapter, copilot_home

OURS = {"command": "/venv/bin/python", "args": ["-m", "hooks.mcp"], "env": {"MCP_CATEGORIES": "all"}}
FOREIGN = {"command": "node", "args": ["/opt/other/server.js"]}


def test_names():
    assert MCP_SERVER_NAME == "agentihooks"
    assert "hooks-utils" in LEGACY_MCP_SERVER_NAMES


class TestClaude:
    def _seed(self, legacy_entry):
        install._CLAUDE_JSON.write_text(
            json.dumps(
                {
                    "mcpServers": {"hooks-utils": legacy_entry, "other": {"command": "x"}},
                    "projects": {"/p": {"disabledMcpServers": ["hooks-utils", "zeta"]}, "/q": {}},
                }
            )
        )
        state = install._load_state()
        state["managed_mcp_servers"] = ["hooks-utils", "other"]
        install._save_state(state)

    def test_own_legacy_entry_is_replaced_and_toggles_carried(self, monkeypatch):
        self._seed(OURS)
        monkeypatch.setattr(install, "_build_mcp_config", lambda cats: {"mcpServers": {MCP_SERVER_NAME: OURS}})
        install._install_user_mcp("p")
        data = json.loads(install._CLAUDE_JSON.read_text())
        assert "hooks-utils" not in data["mcpServers"]
        assert data["mcpServers"][MCP_SERVER_NAME] == OURS
        assert data["mcpServers"]["other"] == {"command": "x"}
        assert data["projects"]["/p"]["disabledMcpServers"] == ["agentihooks", "zeta"]
        ledger = install._load_state()["managed_mcp_servers"]
        assert "hooks-utils" not in ledger and MCP_SERVER_NAME in ledger

    def test_url_mode_entry_is_recognised_by_its_url(self):
        url = {"type": "http", "url": "http://localhost:8642/mcp"}
        self._seed(url)
        install._migrate_legacy_claude_mcp("http://localhost:8642/mcp")
        assert "hooks-utils" not in json.loads(install._CLAUDE_JSON.read_text())["mcpServers"]

    def test_foreign_entry_under_the_legacy_name_is_kept(self, capsys):
        self._seed(FOREIGN)
        install._migrate_legacy_claude_mcp(None)
        assert json.loads(install._CLAUDE_JSON.read_text())["mcpServers"]["hooks-utils"] == FOREIGN
        assert "does not run agentihooks" in capsys.readouterr().out


class TestCodex:
    def test_legacy_table_is_replaced(self, monkeypatch):
        monkeypatch.delenv("CODEX_HOME", raising=False)
        monkeypatch.setenv("AGENTIHOOKS_MCP_TRANSPORT", "stdio")
        monkeypatch.setattr(install, "_detect_venv", lambda: None)
        home = codex_home()
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.toml").write_text(
            '[mcp_servers.hooks-utils]\ncommand = "/venv/bin/python"\nargs = ["-m", "hooks.mcp"]\n\n'
            '[mcp_servers.keep]\ncommand = "node"\n'
        )
        adapter = CodexAdapter()
        adapter.register_hooks_utils("default")
        table = adapter._load_toml(home / "config.toml")["mcp_servers"]
        assert "hooks-utils" not in table
        assert list(table[MCP_SERVER_NAME]["args"]) == ["-m", "hooks.mcp"]
        assert "keep" in table


class TestCopilot:
    def test_legacy_entry_and_settings_lists_are_renamed(self, monkeypatch):
        monkeypatch.delenv("COPILOT_HOME", raising=False)
        monkeypatch.setenv("AGENTIHOOKS_MCP_TRANSPORT", "stdio")
        monkeypatch.setattr(install, "_detect_venv", lambda: None)
        home = copilot_home()
        home.mkdir(parents=True, exist_ok=True)
        (home / "mcp-config.json").write_text(json.dumps({"mcpServers": {"hooks-utils": OURS, "drawio": FOREIGN}}))
        (home / "settings.json").write_text(json.dumps({"enabledMcpServers": ["hooks-utils"], "model": "x"}))
        CopilotAdapter().register_hooks_utils("smith")
        servers = json.loads((home / "mcp-config.json").read_text())["mcpServers"]
        assert "hooks-utils" not in servers and MCP_SERVER_NAME in servers and "drawio" in servers
        settings = json.loads((home / "settings.json").read_text())
        assert settings["enabledMcpServers"] == ["agentihooks"] and settings["model"] == "x"

    def test_legacy_name_in_always_enabled_directive_still_exempts(self, monkeypatch):
        monkeypatch.delenv("COPILOT_HOME", raising=False)
        adapter = CopilotAdapter()
        adapter.write_settings({"_agentihooks": {"mcpDefaultDisabled": True, "mcpAlwaysEnabled": ["hooks-utils"]}})
        path = copilot_home() / "mcp-config.json"
        path.write_text(json.dumps({"mcpServers": {n: {"command": "/bin/true"} for n in ("agentihooks", "drawio")}}))
        adapter.post_install_reconcile([], "smith")
        assert json.loads((copilot_home() / "settings.json").read_text())["disabledMcpServers"] == ["drawio"]


class TestConditionGateNames:
    @pytest.mark.parametrize("tool", ["mcp__agentihooks__condition_set", "mcp__hooks-utils__condition_clear"])
    def test_both_names_are_gated(self, tool):
        from hooks.context.conditions import GATE_MESSAGE, write_guard

        assert write_guard(tool, {}, "sid-rename") == GATE_MESSAGE


class TestCredentialGuardKeepsHeredocs:
    def test_recursive_grep_rewrite_preserves_heredoc_body(self):
        from hooks.context.credential_guard import evaluate

        command = "python3 - <<'EOF'\nprint('keep me')\nEOF\ngrep -rn needle ."
        verdict = evaluate({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": "/tmp"}, allow_rewrite=True)
        rewritten = verdict.rewrite["command"]
        assert "print('keep me')\nEOF\n" in rewritten
        assert "<<HEREDOC" not in rewritten and "AHHEREDOC" not in rewritten
        assert "--exclude=.env" in rewritten and rewritten.endswith("-rn needle .")
