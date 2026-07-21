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


class TestManagedRootScope:
    """A registered source is trusted through its asset dirs, not wholesale."""

    def _register_shallow_profile(self, home, monkeypatch):
        """Register a workspace root — one level too shallow, a real fat-finger."""
        workspace = home / "dev" / "workspace"
        (workspace / "some-other-repo").mkdir(parents=True)
        monkeypatch.setattr(install, "AGENTIHOOKS_ROOT", home / "repos" / "agentihooks")
        install._save_state(
            {"linked_profiles": [{"name": "anton", "path": str(workspace)}], "managed_links": []}
        )
        return workspace

    def test_shallow_root_does_not_claim_unrelated_symlink(self, home, monkeypatch):
        workspace = self._register_shallow_profile(home, monkeypatch)
        target = workspace / "some-other-repo" / "tool.py"
        target.write_text("# not agentihooks' business\n")
        skills = install.CLAUDE_HOME / "skills"
        skills.mkdir(parents=True, exist_ok=True)
        (skills / "my-own-tool").symlink_to(target)

        assert install._link_is_managed(skills / "my-own-tool") is False
        install._remove_agentihooks_symlinks(skills, "skill")
        assert (skills / "my-own-tool").is_symlink(), "a too-shallow registered root claimed an operator link"

    def test_shallow_root_still_claims_its_own_asset_dir(self, home, monkeypatch):
        """Narrowing must not strand the links agentihooks really did create."""
        workspace = self._register_shallow_profile(home, monkeypatch)
        src = workspace / ".claude" / "skills" / "ours"
        src.mkdir(parents=True)
        skills = install.CLAUDE_HOME / "skills"
        skills.mkdir(parents=True, exist_ok=True)
        (skills / "ours").symlink_to(src)

        assert install._link_is_managed(skills / "ours") is True

    def test_root_claims_nested_profile_asset_dir(self, home, monkeypatch):
        """<root>/profiles/<name>/.claude/<kind>/ is the bundle's link shape."""
        workspace = self._register_shallow_profile(home, monkeypatch)
        src = workspace / "profiles" / "anton" / ".claude" / "rules" / "a.md"
        src.parent.mkdir(parents=True)
        src.write_text("rule\n")
        rules = install.CLAUDE_HOME / "rules"
        rules.mkdir(parents=True, exist_ok=True)
        (rules / "a.md").symlink_to(src)

        assert install._link_is_managed(rules / "a.md") is True

    def test_shallow_root_does_not_claim_foreign_claude_md(self, home, monkeypatch):
        workspace = self._register_shallow_profile(home, monkeypatch)
        # A profiles/ dir of the operator's own, beneath the too-shallow root:
        # both ownership signals the old predicate used are present here.
        foreign = workspace / "some-other-repo" / "profiles" / "work" / "CLAUDE.md"
        foreign.parent.mkdir(parents=True)
        foreign.write_text("operator's own\n")
        cm = install.CLAUDE_HOME / "CLAUDE.md"
        cm.symlink_to(foreign)

        assert install._claude_md_is_managed(cm) is False


class TestPreLedgerLinks:
    def test_link_into_managed_root_is_ours_without_a_ledger_entry(self, home, monkeypatch):
        """Links from installs predating the ledger stay reapable."""
        root = home / "repos" / "agentihooks"
        dead_source = root / ".claude" / "rules" / "gone.md"
        dead_source.parent.mkdir(parents=True)
        monkeypatch.setattr(install, "AGENTIHOOKS_ROOT", root)
        rules = install.CLAUDE_HOME / "rules"
        rules.mkdir(parents=True, exist_ok=True)
        (rules / "gone.md").symlink_to(dead_source)  # source already deleted
        install._save_state({"managed_links": []})

        assert install._link_is_managed(rules / "gone.md") is True
        install._remove_agentihooks_symlinks(rules, "rule")
        assert not (rules / "gone.md").is_symlink()


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
