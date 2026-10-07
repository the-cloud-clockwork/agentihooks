import copy
import json
import re

import install as fixture_install
import pytest

from scripts import install
from scripts.targets.claude_target import settings_document

TOKEN = "ghp_" + "d" * 36


def test_profile_env_folds_under_explicit_env():
    rendered = {"_agentihooks": {"env": {"A": "profile", "B": "profile"}}, "env": {"B": "explicit"}, "model": "opus"}
    original = copy.deepcopy(rendered)

    assert settings_document(rendered) == {"env": {"A": "profile", "B": "explicit"}, "model": "opus"}
    assert rendered == original


@pytest.fixture(autouse=True)
def isolate_canonical_installer(_isolate_real_user_paths, monkeypatch):
    for name in ("CLAUDE_HOME", "AGENTIHOOKS_STATE_DIR", "STATE_JSON", "_CLAUDE_JSON", "_BASHRC", "AGENTIHOOKS_ROOT"):
        monkeypatch.setattr(install, name, getattr(fixture_install, name))
    assert install.CLAUDE_HOME == fixture_install.CLAUDE_HOME
    assert install.STATE_JSON == fixture_install.STATE_JSON


def test_profile_env_without_explicit_env():
    assert settings_document({"_agentihooks": {"env": {"A": "1"}}}) == {"env": {"A": "1"}}


def test_marker_without_env_leaves_settings_alone():
    assert settings_document({"_agentihooks": {}, "model": "opus"}) == {"model": "opus"}


def test_credential_literal_dropped_and_reference_kept(capsys):
    assert settings_document({"env": {"LEAKED": TOKEN, "SAFE": "${REF}"}}) == {"env": {"SAFE": "${REF}"}}
    printed = capsys.readouterr().out
    assert "settings env var 'LEAKED' looks like a credential (github_token) — dropped from settings.json." in printed
    assert "SAFE" not in printed


def test_env_removed_when_every_value_is_a_credential():
    assert settings_document({"env": {"LEAKED": TOKEN}, "model": "opus"}) == {"model": "opus"}


def test_empty_or_non_dict_env_passes_through():
    assert settings_document({"env": {}}) == {"env": {}}
    assert settings_document({"env": ["A=" + TOKEN]}) == {"env": ["A=" + TOKEN]}


def test_strict_patterns_apply_and_every_hit_is_named(capsys):
    slack = "xoxb-" + "1" * 12
    assert settings_document({"env": {"SLACK": slack, "BOTH": f"{TOKEN} {slack}"}}) == {}
    printed = capsys.readouterr().out
    assert "settings env var 'SLACK' looks like a credential (" in printed
    assert re.search(
        r"settings env var 'BOTH' looks like a credential \([a-z_]+, [a-z_]+\) — dropped from settings\.json\.", printed
    )
    assert printed.count("Export it in the shell environment instead of writing it to disk.") == 2


def test_write_settings_writes_the_settings_document():
    from scripts.targets._common import _install_module
    from scripts.targets.claude_target import ClaudeAdapter

    path = ClaudeAdapter().write_settings({"_agentihooks": {"env": {"A": "1"}}, "env": {"LEAKED": TOKEN}})

    assert path == _install_module().CLAUDE_HOME / "settings.json"
    assert json.loads(path.read_text())["env"] == {"A": "1"}


def test_write_settings_keeps_operator_plugins_and_enables_fleet_plugins(tmp_path):
    from scripts.targets._common import _install_module
    from scripts.targets.claude_target import ClaudeAdapter

    _i = _install_module()
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    plugin = {"kind": "claude-plugin", "check": ["true"], "install": ["true"]}
    deps = [{**plugin, "id": "fleet@m"}, {**plugin, "id": "muted@m"}, {**plugin, "id": "gone@m", "state": "absent"}]
    (bundle / "deps.json").write_text(json.dumps({"deps": deps}))
    _i._save_state({"bundle": {"path": str(bundle)}})
    operator = {"mine@m": True, "off@m": False, "fleet@m": False, "layer@m": False}
    existing = {_i.MANAGED_BY_KEY: _i.MANAGED_BY_VALUE, "enabledPlugins": operator}
    _i.CLAUDE_HOME.mkdir(parents=True, exist_ok=True)
    (_i.CLAUDE_HOME / "settings.json").write_text(json.dumps(existing))

    path = ClaudeAdapter().write_settings({"enabledPlugins": {"layer@m": True, "muted@m": False}})

    assert json.loads(path.read_text())["enabledPlugins"] == {
        "mine@m": True,
        "off@m": False,
        "fleet@m": True,
        "layer@m": True,
        "muted@m": False,
    }


def test_installed_claude_rules_stay_inside_global_home(tmp_path):
    from scripts.targets.claude_target import ClaudeAdapter

    source = tmp_path / "rules"
    source.mkdir()
    (source / "rule.md").write_text("GLOBAL RULE\n")
    rules = install.CLAUDE_HOME / "rules"
    rules.mkdir(parents=True)
    (rules / "rule.md").symlink_to(source / "rule.md")
    install._state_record_link(rules / "rule.md", source / "rule.md", "rules")
    ClaudeAdapter().install_features("rules", [("rule", source)], lambda path: path.suffix == ".md")

    rule = install.CLAUDE_HOME / "rules" / "rule.md"
    assert rule.resolve().is_relative_to(install.CLAUDE_HOME)
    assert rule.read_text() == "GLOBAL RULE\n"
    entry = install._state_links()[str(rule)]
    assert entry["target"] == str(source / "rule.md")
    assert entry["kind"] == "rules"


def test_empty_claude_rule_install_keeps_user_files(tmp_path):
    from scripts.targets.claude_target import ClaudeAdapter, refresh_rules

    rules = install.CLAUDE_HOME / "rules"
    rules.mkdir(parents=True)
    (rules / "user.md").write_text("USER RULE\n")
    ClaudeAdapter().install_features("rules", [("rule", tmp_path / "absent")], lambda path: path.suffix == ".md")

    assert (rules / "user.md").read_text() == "USER RULE\n"
    assert install._state_links() == {}
    assert "USER RULE" in refresh_rules(
        rules, install.CLAUDE_HOME / "CLAUDE.md", install.CLAUDE_HOME / "CLAUDE.local.md", False
    )


def test_installed_claude_rules_refresh_and_preserve_foreign_files(tmp_path, capsys):
    from scripts.targets.claude_target import ClaudeAdapter

    adapter = ClaudeAdapter()
    source = tmp_path / "source"
    source.mkdir()
    (source / "stale.md").write_text("STALE\n")
    adapter.install_features("rules", [("rule", source)], lambda path: path.suffix == ".md")
    (source / "stale.md").unlink()
    (source / "rule.md").write_text("BASE\n")
    (source / ".hidden.md").write_text("HIDDEN\n")
    (source / "notes.txt").write_text("NOT A RULE\n")
    role = tmp_path / "role"
    role.mkdir()
    (role / "rule.md").write_text("---\npaths: ['**/*.py']\n---\nROLE\n")
    rules = install.CLAUDE_HOME / "rules"
    (rules / "operator.md").write_text("OPERATOR\n")
    (role / "operator.md").write_text("DO NOT OVERWRITE\n")
    (rules / "foreign.md").symlink_to(tmp_path / "unmounted" / "foreign.md")
    (role / "foreign.md").write_text("DO NOT OVERWRITE\n")

    adapter.install_features(
        "rules", [("rule", source), ("rule", role), ("rule", tmp_path / "absent")], lambda path: path.suffix == ".md"
    )

    assert "Removed managed rule: stale.md" in capsys.readouterr().out
    assert not (rules / "stale.md").exists()
    assert not (rules / ".hidden.md").exists()
    assert not (rules / "notes.txt").exists()
    assert (rules / "rule.md").read_text() == "---\npaths: ['**/*.py']\n---\nROLE\n"
    assert (rules / "operator.md").read_text() == "OPERATOR\n"
    assert (rules / "foreign.md").is_symlink()
    assert install._remove_agentihooks_symlinks(rules, "rule") == 1
    assert not (rules / "rule.md").exists()
    assert (rules / "operator.md").read_text() == "OPERATOR\n"
    assert (rules / "foreign.md").is_symlink()


def test_claude_rule_rerun_keeps_unchanged_copies_and_removes_only_dropped_rules(tmp_path, capsys):
    from scripts.targets.claude_target import ClaudeAdapter

    adapter = ClaudeAdapter()
    source = tmp_path / "source"
    source.mkdir()
    for name in ("keep.md", "edit.md", "drop.md", "folder.md"):
        (source / name).write_text(f"{name}\n")
    rules = install.CLAUDE_HOME / "rules"
    (rules / "folder.md").mkdir(parents=True)
    install._state_record_link(rules / "folder.md", source / "folder.md", "rules")
    adapter.install_features("rules", [("rule", source)], lambda path: path.suffix == ".md")
    assert (rules / "folder.md").is_dir()
    (source / "folder.md").unlink()
    inodes = {name: (rules / name).stat().st_ino for name in ("keep.md", "edit.md", "drop.md")}
    capsys.readouterr()

    adapter.install_features("rules", [("rule", source)], lambda path: path.suffix == ".md")

    assert capsys.readouterr().out == ""
    assert {name: (rules / name).stat().st_ino for name in inodes} == inodes

    (source / "drop.md").unlink()
    (source / "edit.md").write_text("EDITED\n")
    adapter.install_features("rules", [("rule", source)], lambda path: path.suffix == ".md")

    out = capsys.readouterr().out
    assert out.count("Removed managed rule") == 1
    assert "  [RM] Removed managed rule: drop.md" in out
    assert not (rules / "drop.md").exists()
    assert str(rules / "drop.md") not in install._state_links()
    assert (rules / "keep.md").stat().st_ino == inodes["keep.md"]
    assert (rules / "edit.md").read_text() == "EDITED\n"
    assert sorted(path.name for path in rules.iterdir()) == ["edit.md", "folder.md", "keep.md"]
    assert install._state_links()[str(rules / "keep.md")]["rule_sources"] == [str(source)]
    assert install._state_links()[str(rules / "edit.md")]["rule_sources"] == [str(source)]


def test_uninstall_removes_copied_claude_rules_as_last_artifact(tmp_path, monkeypatch, capsys):
    from argparse import Namespace
    from types import SimpleNamespace

    from scripts.targets.claude_target import ClaudeAdapter

    source = tmp_path / "rules"
    source.mkdir()
    (source / "rule.md").write_text("RULE\n")
    ClaudeAdapter().install_features("rules", [("rule", source)], lambda path: path.suffix == ".md")
    monkeypatch.setattr(install, "_cli_tool_is_installed", lambda: False)
    monkeypatch.setattr(install, "_uninstall_cli_tool", lambda: None)
    monkeypatch.setattr(install, "_remove_systemd_user_unit", lambda: None)
    daemon = SimpleNamespace(read_pidfile=lambda: {}, stop=lambda: False, pid_alive=lambda pid: False)
    monkeypatch.setattr(install, "_mcp_daemon_module", lambda: daemon)

    rules = install.CLAUDE_HOME / "rules"
    (rules / "user-one.md").write_text("USER ONE\n")
    (rules / "user-two.md").write_text("USER TWO\n")
    install.uninstall_global(Namespace(yes=True))

    assert f"  {rules}/  → 1 rule(s)" in capsys.readouterr().out
    assert not (rules / "rule.md").exists()
    assert (rules / "user-one.md").read_text() == "USER ONE\n"
    assert (rules / "user-two.md").read_text() == "USER TWO\n"


def test_refresh_rules_rewrites_copied_source_before_delivery(tmp_path, monkeypatch):
    from argparse import Namespace

    from scripts.targets.claude_target import ClaudeAdapter

    source = tmp_path / "rules"
    source.mkdir()
    (source / "rule.md").write_text("OLD RULE\n")
    ClaudeAdapter().install_features("rules", [("rule", source)], lambda path: path.suffix == ".md")
    (source / "rule.md").write_text("NEW RULE\n")
    (install.CLAUDE_HOME / "CLAUDE.md").write_text("GLOBAL PERSONA\n")
    (install.CLAUDE_HOME / "CLAUDE.local.md").write_text("LOCAL OVERRIDE\n")
    rules = install.CLAUDE_HOME / "rules"
    (rules / "folder").mkdir()
    foreign = tmp_path / "foreign" / "rule.md"
    foreign.parent.mkdir()
    foreign.write_text("FOREIGN RULE\n")
    (rules / "linked.md").symlink_to(foreign)
    outside = install.CLAUDE_HOME / "commands" / "copied.md"
    outside.parent.mkdir()
    outside.write_text("COMMAND\n")
    install._state_record_links(
        [
            (rules / "folder", source / "rule.md", "rules"),
            (rules / "linked.md", source / "rule.md", "rules"),
            (outside, source / "rule.md", "commands"),
        ]
    )
    seen = []

    def capture(profile, payload):
        seen.append(payload)
        return {"marker_path": str(tmp_path / "marker"), "pending_count": 0, "content_hash": "proof"}

    monkeypatch.setattr("hooks.context.rules_refresh.write_refresh_marker", capture)
    install._cmd_refresh_rules(Namespace(profile="engineer", clear=False, dry_run=True))
    assert (install.CLAUDE_HOME / "rules" / "rule.md").read_text() == "OLD RULE\n"
    install._cmd_refresh_rules(Namespace(profile="engineer", clear=False, dry_run=False))

    assert (install.CLAUDE_HOME / "rules" / "rule.md").read_text() == "NEW RULE\n"
    assert "NEW RULE" in seen[0]
    assert "GLOBAL PERSONA" in seen[0]
    assert "LOCAL OVERRIDE" in seen[0]
    assert "OLD RULE" not in seen[0]
    assert (rules / "folder").is_dir()
    assert (rules / "linked.md").is_symlink()
    assert outside.read_text() == "COMMAND\n"

    (source / "rule.md").rename(source / "renamed.md")
    (source / "added.md").write_text("ADDED RULE\n")
    (source / "README.md").write_text("NOT A RULE\n")
    install._cmd_refresh_rules(Namespace(profile="engineer", clear=False, dry_run=False))
    assert not (rules / "rule.md").exists()
    assert (rules / "renamed.md").read_text() == "NEW RULE\n"
    assert (rules / "added.md").read_text() == "ADDED RULE\n"
    assert not (rules / "README.md").exists()
    assert "NEW RULE" in seen[1]
    assert "ADDED RULE" in seen[1]


def test_refresh_ignores_sources_of_a_foreign_retargeted_rule(tmp_path):
    from scripts.targets.claude_target import ClaudeAdapter, refresh_rules

    adapter = ClaudeAdapter()
    old = tmp_path / "old"
    old.mkdir()
    (old / "aaa.md").write_text("OLD PROFILE\n")
    adapter.install_features("rules", [("rule", old)], lambda path: path.suffix == ".md")
    rules = install.CLAUDE_HOME / "rules"
    (rules / "aaa.md").unlink()
    foreign = tmp_path / "foreign.md"
    foreign.write_text("FOREIGN RULE\n")
    (rules / "aaa.md").symlink_to(foreign)
    current = tmp_path / "current"
    current.mkdir()
    (current / "rule.md").write_text("CURRENT PROFILE\n")
    adapter.install_features("rules", [("rule", current)], lambda path: path.suffix == ".md")
    (current / "rule.md").write_text("UPDATED PROFILE\n")

    payload = refresh_rules(rules, install.CLAUDE_HOME / "CLAUDE.md", install.CLAUDE_HOME / "CLAUDE.local.md", False)

    assert (rules / "aaa.md").is_symlink()
    assert (rules / "rule.md").read_text() == "UPDATED PROFILE\n"
    assert "UPDATED PROFILE" in payload


def _seed_managed_mcp(monkeypatch, current):
    install._CLAUDE_JSON.parent.mkdir(parents=True, exist_ok=True)
    install._CLAUDE_JSON.write_text(json.dumps({"mcpServers": {"keep": {}, "stale": {}, "hand": {}}}))
    install.STATE_JSON.parent.mkdir(parents=True, exist_ok=True)
    install.STATE_JSON.write_text(json.dumps({"managed_mcp_servers": ["keep", "stale"]}))
    monkeypatch.setattr(fixture_install, "_collect_all_managed_mcp_servers", lambda: {name: {} for name in current})


def test_inheriting_profile_still_prunes_stale_mcp_servers(monkeypatch):
    from scripts.targets.claude_target import ClaudeAdapter

    _seed_managed_mcp(monkeypatch, {"keep"})

    ClaudeAdapter().post_install_reconcile(["parent", "child"], "child")

    assert set(json.loads(install._CLAUDE_JSON.read_text())["mcpServers"]) == {"keep", "hand"}


def test_missing_profile_skips_reconcile_and_is_named(monkeypatch, capsys):
    from scripts.targets.claude_target import ClaudeAdapter

    _seed_managed_mcp(monkeypatch, {"keep"})

    ClaudeAdapter().post_install_reconcile(["parent", "child"], "child,gone,lost")

    assert set(json.loads(install._CLAUDE_JSON.read_text())["mcpServers"]) == {"keep", "stale", "hand"}
    assert (
        "  [--] Skipping MCP ledger reconcile — profile(s) gone, lost did not resolve this run "
        "(transient source loss); ledger left unchanged."
    ) in capsys.readouterr().out
