import copy
import json
import re

from scripts.targets.claude_target import settings_document

TOKEN = "ghp_" + "d" * 36


def test_profile_env_folds_under_explicit_env():
    rendered = {"_agentihooks": {"env": {"A": "profile", "B": "profile"}}, "env": {"B": "explicit"}, "model": "opus"}
    original = copy.deepcopy(rendered)

    assert settings_document(rendered) == {"env": {"A": "profile", "B": "explicit"}, "model": "opus"}
    assert rendered == original


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


def test_installed_claude_rules_stay_inside_global_home(tmp_path):
    from scripts.targets._common import _install_module
    from scripts.targets.claude_target import ClaudeAdapter

    install = _install_module()
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


def test_installed_claude_rules_refresh_and_preserve_foreign_files(tmp_path, capsys):
    from scripts.targets._common import _install_module
    from scripts.targets.claude_target import ClaudeAdapter

    install = _install_module()
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


def test_uninstall_removes_copied_claude_rules_as_last_artifact(tmp_path, monkeypatch, capsys):
    from argparse import Namespace
    from types import SimpleNamespace

    from scripts.targets._common import _install_module
    from scripts.targets.claude_target import ClaudeAdapter

    install = _install_module()
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
