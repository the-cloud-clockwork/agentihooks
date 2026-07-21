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
        install._save_state({"linked_profiles": [{"name": "anton", "path": str(workspace)}], "managed_links": []})
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

    def test_early_exit_accounts_for_bashrc_block(self, home, monkeypatch):
        """A bashrc block alone is still an install — do not report false-clean."""
        import argparse

        install._BASHRC.write_text(f"export FOO=1\n{install._BLOCK_START}\nexport BAR=2\n{install._BLOCK_END}\n")
        monkeypatch.setattr(install, "_cli_tool_is_installed", lambda: False)
        monkeypatch.setattr(install, "_uninstall_cli_tool", lambda: None)

        install.uninstall_global(argparse.Namespace(yes=True))

        assert install._BLOCK_START not in install._BASHRC.read_text(), (
            "uninstall returned early and left the bashrc block behind"
        )


class TestLedgerWriteSide:
    """The removal tests all seed the ledger by hand; this exercises the writer."""

    def test_linking_records_the_link(self, home, monkeypatch):
        src_dir = home / "src" / "skills"
        (src_dir / "alpha").mkdir(parents=True)
        (src_dir / "beta").mkdir()
        dst = install.CLAUDE_HOME / "skills"

        install._symlink_dir_contents(src_dir, dst, label="skill")

        ledger = install._state_links()
        assert set(ledger) == {str(dst / "alpha"), str(dst / "beta")}
        entry = ledger[str(dst / "alpha")]
        assert entry["target"] == str(src_dir / "alpha")
        assert entry["kind"] == "skills"

    def test_removal_forgets_the_entry(self, home, monkeypatch):
        src_dir = home / "src" / "skills"
        (src_dir / "alpha").mkdir(parents=True)
        dst = install.CLAUDE_HOME / "skills"
        install._symlink_dir_contents(src_dir, dst, label="skill")

        install._remove_agentihooks_symlinks(dst, "skill")

        assert install._state_links() == {}, "ledger kept an entry for a link it removed"


class TestSeparatorGuard:
    """`_under_root` is what stops a sibling segment reading as a child."""

    def test_profiles_sibling_is_not_claimed(self, home, monkeypatch):
        root = home / "repos" / "agentihooks"
        monkeypatch.setattr(install, "AGENTIHOOKS_ROOT", root)
        # `profiles-backup` shares a prefix with `profiles` but is not under it.
        foreign = root / "profiles-backup" / "anton" / "CLAUDE.md"
        foreign.parent.mkdir(parents=True)
        foreign.write_text("a copy the operator keeps\n")
        cm = install.CLAUDE_HOME / "CLAUDE.md"
        cm.symlink_to(foreign)

        assert install._claude_md_is_managed(cm) is False

    def test_real_profiles_dir_is_claimed(self, home, monkeypatch):
        root = home / "repos" / "agentihooks"
        monkeypatch.setattr(install, "AGENTIHOOKS_ROOT", root)
        src = root / "profiles" / "anton" / "CLAUDE.md"
        src.parent.mkdir(parents=True)
        src.write_text("ours\n")
        cm = install.CLAUDE_HOME / "CLAUDE.md"
        cm.symlink_to(src)

        assert install._claude_md_is_managed(cm) is True


class TestSettingsRestore:
    def test_uninstall_restores_the_pre_agentihooks_settings(self, home, monkeypatch):
        import argparse

        original = '{"permissions": {"allow": ["Bash(ls:*)"]}}'
        backup = install.CLAUDE_HOME / "settings.json.bak.20260101_000000"
        backup.write_text(original)
        settings = install.CLAUDE_HOME / "settings.json"
        settings.write_text(json.dumps({install.MANAGED_BY_KEY: install.MANAGED_BY_VALUE, "model": "opus"}))
        install._save_state({"settings_original_backup": str(backup)})
        monkeypatch.setattr(install, "_cli_tool_is_installed", lambda: False)
        monkeypatch.setattr(install, "_uninstall_cli_tool", lambda: None)

        install.uninstall_global(argparse.Namespace(yes=True))

        assert settings.read_text() == original, "uninstall did not restore the operator's settings.json"
        assert "settings_original_backup" not in install._load_state()

    def test_backup_records_only_the_first_unmanaged_file(self, home, monkeypatch):
        """Later unmanaged files are not the pre-agentihooks original."""
        import datetime as _dt

        class _Clock(_dt.datetime):
            _n = 0

            @classmethod
            def now(cls, tz=None):
                cls._n += 1  # distinct second per call, so backups get distinct names
                return _dt.datetime(2026, 1, 1, 0, 0, cls._n, tzinfo=tz)

        monkeypatch.setattr(install, "datetime", _Clock)
        settings = install.CLAUDE_HOME / "settings.json"

        settings.write_text('{"model": "the-original"}')
        install._backup_settings(settings)
        first = install._load_state()["settings_original_backup"]

        # A later init sees an unmanaged file again — the operator overwrote
        # settings.json by hand since. That is not the pre-agentihooks original.
        settings.write_text('{"model": "written-later"}')
        install._backup_settings(settings)

        assert install._load_state()["settings_original_backup"] == first
        assert json.loads(Path(first).read_text()) == {"model": "the-original"}


class TestPruneUnderUncertainty:
    """Deleting on incomplete information is the bug class, not a fallback."""

    def test_orphan_sweep_skipped_when_sources_are_unresolvable(self, home, monkeypatch):
        _write_claude_json({"hooks-utils": {"command": "python"}, "gateway": {"command": "x"}})
        install._save_state({"managed_mcp_servers": ["hooks-utils", "gateway"]})
        # A moved venv makes _build_mcp_config sys.exit(1) deep inside the collector.
        monkeypatch.setattr(install, "_collect_all_managed_mcp_servers", lambda: (_ for _ in ()).throw(SystemExit(1)))

        install._prune_stale_mcp_servers(install.AGENTIHOOKS_STATE_DIR / "known-mcp-servers.json")

        assert set(_read_claude_json()) == {"hooks-utils", "gateway"}, (
            "prune deleted the whole ledger because it could not resolve what the chain defines"
        )

    def test_systemexit_does_not_escape(self, home, monkeypatch):
        monkeypatch.setattr(install, "_collect_all_managed_mcp_servers", lambda: (_ for _ in ()).throw(SystemExit(1)))
        assert install._get_managed_mcp_names() is None


class TestDisabledListPreservation:
    def test_disable_preference_survives_for_a_known_server(self, home, monkeypatch):
        """A server Claude Code still knows about must keep its disable entry."""
        install._CLAUDE_JSON.write_text(
            json.dumps(
                {
                    "mcpServers": {"live": {"command": "x"}},
                    "claudeAiMcpEverConnected": ["parked"],
                    "projects": {"/p": {"disabledMcpServers": ["parked", "live"]}},
                }
            )
        )
        install._save_state({"managed_mcp_servers": []})
        monkeypatch.setattr(install, "_get_managed_mcp_names", lambda: set())

        install._prune_stale_mcp_servers(install.AGENTIHOOKS_STATE_DIR / "known-mcp-servers.json")

        disabled = json.loads(install._CLAUDE_JSON.read_text())["projects"]["/p"]["disabledMcpServers"]
        assert "parked" in disabled, "prune destroyed a disable preference for a server Claude Code knows"

    def test_wholly_unknown_disable_entry_is_still_pruned(self, home, monkeypatch):
        """The widening must not make step 1 inert."""
        install._CLAUDE_JSON.write_text(
            json.dumps(
                {
                    "mcpServers": {"live": {"command": "x"}},
                    "projects": {"/p": {"disabledMcpServers": ["ghost", "live"]}},
                }
            )
        )
        install._save_state({"managed_mcp_servers": []})
        monkeypatch.setattr(install, "_get_managed_mcp_names", lambda: set())

        install._prune_stale_mcp_servers(install.AGENTIHOOKS_STATE_DIR / "known-mcp-servers.json")

        disabled = json.loads(install._CLAUDE_JSON.read_text())["projects"]["/p"]["disabledMcpServers"]
        assert disabled == ["live"]


class TestMovedSourceOwnership:
    """A source that moved is still a source — ownership must not evaporate."""

    def test_claude_md_symlink_survives_a_renamed_bundle(self, home, monkeypatch):
        monkeypatch.setattr(install, "AGENTIHOOKS_ROOT", home / "repos" / "agentihooks")
        bundle = home / "bundle"
        src = bundle / "profiles" / "anton" / "CLAUDE.md"
        src.parent.mkdir(parents=True)
        src.write_text("profile prompt\n")
        install._save_state({"bundle": {"path": str(bundle)}})
        cm = install.CLAUDE_HOME / "CLAUDE.md"
        cm.symlink_to(src)
        assert install._claude_md_is_managed(cm) is True

        bundle.rename(home / "bundle-renamed")  # operator moves the checkout

        assert install._claude_md_is_managed(cm) is True, (
            "a renamed bundle stranded agentihooks' own CLAUDE.md as unowned"
        )

    def test_link_into_renamed_bundle_is_still_ours(self, home, monkeypatch):
        monkeypatch.setattr(install, "AGENTIHOOKS_ROOT", home / "repos" / "agentihooks")
        bundle = home / "bundle"
        src = bundle / ".claude" / "skills" / "s"
        src.mkdir(parents=True)
        install._save_state({"bundle": {"path": str(bundle)}, "managed_links": []})
        skills = install.CLAUDE_HOME / "skills"
        skills.mkdir(parents=True, exist_ok=True)
        (skills / "s").symlink_to(src)

        bundle.rename(home / "bundle-renamed")

        assert install._link_is_managed(skills / "s") is True


class TestCleanWithoutStateDir:
    def test_claude_home_cleaned_when_state_dir_is_absent(self, home):
        """~/.agentihooks and ~/.claude fail independently."""
        settings = install.CLAUDE_HOME / "settings.json"
        settings.write_text(json.dumps({install.MANAGED_BY_KEY: install.MANAGED_BY_VALUE}))
        import shutil

        shutil.rmtree(install.AGENTIHOOKS_STATE_DIR)

        install._clean_state_dir()

        assert not settings.exists(), "a missing state dir stranded a managed settings.json"


class TestMcpNameCollision:
    """A profile defining a name the operator already uses must not take it over."""

    def _collide(self):
        _write_claude_json({"shared": {"command": "operator-binary"}})
        install._save_state({"managed_mcp_servers": []})
        install._merge_mcp_to_user_scope({"shared": {"command": "profile-binary"}})

    def test_operator_config_is_not_overwritten(self, home):
        self._collide()
        assert _read_claude_json()["shared"] == {"command": "operator-binary"}

    def test_collided_name_is_never_claimed(self, home):
        self._collide()
        install._reconcile_managed_mcp_ledger({"shared", "hooks-utils"})
        assert "shared" not in install._load_state()["managed_mcp_servers"]

    def test_dropping_the_profile_does_not_delete_operator_server(self, home):
        self._collide()
        install._reconcile_managed_mcp_ledger({"shared", "hooks-utils"})
        install._reconcile_managed_mcp_ledger({"hooks-utils"})  # profile drops it
        assert "shared" in _read_claude_json(), "the operator's MCP server was deleted by a profile update"

    def test_uninstall_does_not_claim_by_name_match(self, home, monkeypatch):
        """The ledger decides, not what a profile happens to define right now."""
        import argparse

        _write_claude_json({"operator-own": {"command": "theirs"}})
        install._save_state({"managed_mcp_servers": []})
        monkeypatch.setattr(
            install, "_collect_all_managed_mcp_servers", lambda: {"operator-own": {"command": "profile"}}
        )
        monkeypatch.setattr(install, "_cli_tool_is_installed", lambda: False)
        monkeypatch.setattr(install, "_uninstall_cli_tool", lambda: None)

        install.uninstall_global(argparse.Namespace(yes=True))

        assert "operator-own" in _read_claude_json(), "uninstall deleted a server it never installed"
