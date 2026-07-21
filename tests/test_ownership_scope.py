"""Ownership-scope probes: agentihooks must only remove what it installed.

Every artifact agentihooks creates in ``~/.claude`` is recorded in
``state.json``. prune, uninstall and init must treat that ledger as the sole
authority for what may be removed — a third-party symlink, a hand-added MCP
server, or an operator-owned file must survive all three.
"""

import json
from pathlib import Path

import install
import pytest


@pytest.fixture
def home(tmp_path):
    return Path.home()


def _write_claude_json(servers: dict) -> None:
    install._CLAUDE_JSON.write_text(json.dumps({"mcpServers": servers, "projects": {}}, indent=2))


def _read_claude_json() -> dict:
    return json.loads(install._CLAUDE_JSON.read_text())["mcpServers"]


class TestPruneScope:
    def test_prune_keeps_hand_added_mcp_servers(self, home, monkeypatch):
        """A server the operator added with `claude mcp add` is not agentihooks'."""
        _write_claude_json(
            {
                "hooks-utils": {"command": "python"},
                "operator-own": {"command": "node", "args": ["server.js"]},
            }
        )
        install._save_state({"managed_mcp_servers": ["hooks-utils"]})
        monkeypatch.setattr(install, "_get_managed_mcp_names", lambda: {"hooks-utils"})

        install._prune_stale_mcp_servers(install.AGENTIHOOKS_STATE_DIR / "known-mcp-servers.json")

        assert "operator-own" in _read_claude_json(), "prune deleted a server agentihooks never installed"

    def test_prune_removes_own_stale_server(self, home, monkeypatch):
        """A server in the ledger but no longer in any source is agentihooks' to remove."""
        _write_claude_json({"hooks-utils": {"command": "python"}, "dropped": {"command": "x"}})
        install._save_state({"managed_mcp_servers": ["hooks-utils", "dropped"]})
        monkeypatch.setattr(install, "_get_managed_mcp_names", lambda: {"hooks-utils"})

        install._prune_stale_mcp_servers(install.AGENTIHOOKS_STATE_DIR / "known-mcp-servers.json")

        servers = _read_claude_json()
        assert "dropped" not in servers, "prune left behind a server agentihooks installed"
        assert "hooks-utils" in servers


class TestCleanScope:
    def test_clean_keeps_foreign_symlinks(self, home):
        """`init --clean` must not flatten ~/.claude asset dirs wholesale."""
        foreign_src = home / "other-tool" / "skills" / "foreign-skill"
        foreign_src.mkdir(parents=True)
        skills = install.CLAUDE_HOME / "skills"
        skills.mkdir(parents=True, exist_ok=True)
        (skills / "foreign-skill").symlink_to(foreign_src)

        install._clean_state_dir()

        assert (skills / "foreign-skill").is_symlink(), "clean removed a third-party skill symlink"

    def test_clean_keeps_settings_local(self, home):
        """settings.local.json is operator-owned — agentihooks never writes it."""
        local = install.CLAUDE_HOME / "settings.local.json"
        local.write_text('{"permissions": {"allow": ["Bash(ls:*)"]}}')

        install._clean_state_dir()

        assert local.exists(), "clean deleted operator-owned settings.local.json"


class TestStaleLinkScope:
    def test_broken_foreign_symlink_survives(self, home):
        """A dangling link into someone else's tree is not agentihooks' to reap."""
        skills = install.CLAUDE_HOME / "skills"
        skills.mkdir(parents=True, exist_ok=True)
        (skills / "foreign").symlink_to(home / "not-mounted-yet" / "foreign")
        src = home / "src-skills"
        src.mkdir()

        install._cleanup_stale_links(skills, src, None)

        assert (skills / "foreign").is_symlink(), "cleanup reaped a foreign dangling symlink"


class TestUninstallScope:
    def test_uninstall_only_removes_recorded_links(self, home, monkeypatch):
        """Prefix matching must not claim a neighbour repo that shares a name prefix."""
        fake_root = home / "repos" / "agentihooks"
        fake_root.mkdir(parents=True)
        monkeypatch.setattr(install, "AGENTIHOOKS_ROOT", fake_root)
        neighbour = home / "repos" / "agentihooks-extras" / "skills" / "nb"
        neighbour.mkdir(parents=True)
        skills = install.CLAUDE_HOME / "skills"
        skills.mkdir(parents=True, exist_ok=True)
        (skills / "nb").symlink_to(neighbour)
        install._save_state({"managed_links": []})

        install._remove_agentihooks_symlinks(skills, "skill")

        assert (skills / "nb").is_symlink(), "uninstall claimed a link from a prefix-matching neighbour repo"
