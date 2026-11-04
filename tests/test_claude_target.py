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


@pytest.mark.feature("claude-settings-additive-merge")
def test_write_settings_keeps_unmanaged_user_keys_on_first_init():
    from scripts.targets.claude_target import ClaudeAdapter

    install.CLAUDE_HOME.mkdir(parents=True, exist_ok=True)
    settings_path = install.CLAUDE_HOME / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "customTopLevelKey": "operator value",
                "env": {"OPERATOR_VAR": "kept"},
                "permissions": {"allow": ["Bash(ls:*)"]},
                "statusLine": {"type": "command", "command": "operator-status"},
            }
        )
    )

    path = ClaudeAdapter().write_settings({"env": {"NEW_VAR": "1"}, "permissions": {"allow": ["Bash(git:*)"]}})

    written = json.loads(path.read_text())
    assert written["customTopLevelKey"] == "operator value"
    assert written["statusLine"] == {"type": "command", "command": "operator-status"}
    assert written["env"] == {"OPERATOR_VAR": "kept", "NEW_VAR": "1"}
    assert written["permissions"]["allow"] == ["Bash(ls:*)", "Bash(git:*)"]


@pytest.mark.feature("claude-settings-additive-merge")
def test_write_settings_second_run_keeps_a_key_added_between_runs():
    from scripts.targets.claude_target import ClaudeAdapter

    adapter = ClaudeAdapter()
    path = adapter.write_settings({"env": {"A": "1"}})

    doc = json.loads(path.read_text())
    doc["operatorAddedLater"] = "mine"
    path.write_text(json.dumps(doc))

    adapter.write_settings({"env": {"A": "1"}})

    assert json.loads(path.read_text())["operatorAddedLater"] == "mine"


@pytest.mark.feature("claude-settings-additive-merge")
def test_write_settings_merges_siblings_without_dropping_either_side():
    from scripts.targets.claude_target import ClaudeAdapter

    install.CLAUDE_HOME.mkdir(parents=True, exist_ok=True)
    settings_path = install.CLAUDE_HOME / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                install.MANAGED_BY_KEY: install.MANAGED_BY_VALUE,
                "env": {"OPERATOR_VAR": "kept"},
                "permissions": {"allow": ["Bash(ls:*)"]},
            }
        )
    )

    path = ClaudeAdapter().write_settings(
        {"env": {"BUNDLE_VAR": "2"}, "permissions": {"allow": ["Bash(git:*)", "Bash(ls:*)"]}}
    )

    written = json.loads(path.read_text())
    assert written["env"] == {"OPERATOR_VAR": "kept", "BUNDLE_VAR": "2"}
    assert written["permissions"]["allow"] == ["Bash(ls:*)", "Bash(git:*)"]


@pytest.mark.feature("claude-settings-additive-merge")
def test_write_settings_removes_a_key_no_longer_rendered():
    from scripts.targets.claude_target import ClaudeAdapter

    adapter = ClaudeAdapter()
    path = adapter.write_settings({"env": {"FEATURE_FLAG": "on", "KEPT": "1"}})
    assert json.loads(path.read_text())["env"] == {"FEATURE_FLAG": "on", "KEPT": "1"}

    adapter.write_settings({"env": {"KEPT": "1"}})

    assert json.loads(path.read_text())["env"] == {"KEPT": "1"}


@pytest.mark.feature("claude-settings-additive-merge")
def test_additive_merge_removes_a_three_level_leaf_no_longer_rendered():
    from scripts.targets.claude_target import _additive_merge

    existing = {"a": {"b": {"c": 1, "d": 2}}}
    prior_record = {"leaves": ["a.b.c"], "lists": {}}

    merged, record = _additive_merge(existing, {}, prior_record)

    assert merged == {"a": {"b": {"d": 2}}}
    assert record == {"leaves": [], "lists": {}}


@pytest.mark.feature("claude-settings-additive-merge")
def test_additive_merge_removal_of_an_already_absent_leaf_is_a_no_op():
    from scripts.targets.claude_target import _additive_merge

    existing = {"env": {"KEPT": "1"}}
    prior_record = {"leaves": ["env.GONE"], "lists": {}}

    merged, record = _additive_merge(existing, {}, prior_record)

    assert merged == {"env": {"KEPT": "1"}}
    assert record == {"leaves": [], "lists": {}}


@pytest.mark.feature("claude-settings-additive-merge")
def test_additive_merge_prunes_a_container_left_empty_by_a_removed_leaf():
    from scripts.targets.claude_target import _additive_merge

    existing = {"env": {"AGENTIHOOKS_VAR": "1"}, "operatorKey": "kept"}
    prior_record = {"leaves": ["env.AGENTIHOOKS_VAR"], "lists": {}}

    merged, record = _additive_merge(existing, {}, prior_record)

    assert merged == {"operatorKey": "kept"}


@pytest.mark.feature("claude-settings-additive-merge")
def test_additive_merge_keeps_a_container_that_still_holds_an_operator_key():
    from scripts.targets.claude_target import _additive_merge

    existing = {"env": {"AGENTIHOOKS_VAR": "1", "OPERATOR_VAR": "kept"}}
    prior_record = {"leaves": ["env.AGENTIHOOKS_VAR"], "lists": {}}

    merged, record = _additive_merge(existing, {}, prior_record)

    assert merged == {"env": {"OPERATOR_VAR": "kept"}}


@pytest.mark.feature("claude-settings-additive-merge")
def test_additive_merge_prunes_every_ancestor_left_empty_by_a_four_level_leaf():
    from scripts.targets.claude_target import _additive_merge

    existing = {"a": {"b": {"c": {"d": 1}}}}
    prior_record = {"leaves": ["a.b.c.d"], "lists": {}}

    merged, record = _additive_merge(existing, {}, prior_record)

    assert merged == {}


@pytest.mark.feature("claude-settings-additive-merge")
def test_additive_merge_processes_every_rendered_key_after_a_list_value():
    from scripts.targets.claude_target import _additive_merge

    rendered = {"permissions": {"allow": ["Bash(git:*)"], "defaultMode": "bypassPermissions"}}

    merged, record = _additive_merge({}, rendered, {})

    assert merged == {"permissions": {"allow": ["Bash(git:*)"], "defaultMode": "bypassPermissions"}}
    assert record == {"leaves": ["permissions.defaultMode"], "lists": {"permissions.allow": ["Bash(git:*)"]}}


@pytest.mark.feature("claude-settings-additive-merge")
def test_additive_merge_drops_a_stale_rendered_list_item_using_the_prior_record():
    from scripts.targets.claude_target import _additive_merge

    existing = {"permissions": {"allow": ["Bash(ls:*)", "Bash(git:*)"]}}
    rendered = {"permissions": {"allow": ["Bash(npm:*)"]}}
    prior_record = {"leaves": [], "lists": {"permissions.allow": ["Bash(git:*)"]}}

    merged, record = _additive_merge(existing, rendered, prior_record)

    assert merged == {"permissions": {"allow": ["Bash(ls:*)", "Bash(npm:*)"]}}
    assert record == {"leaves": [], "lists": {"permissions.allow": ["Bash(npm:*)"]}}


@pytest.mark.feature("claude-settings-additive-merge")
def test_additive_merge_discards_a_stale_list_entry_when_the_list_stops_rendering():
    from scripts.targets.claude_target import _additive_merge

    existing = {"permissions": {"allow": ["Bash(ls:*)", "Bash(git:*)"]}}
    prior_record = {"leaves": [], "lists": {"permissions.allow": ["Bash(git:*)"]}}

    merged, record = _additive_merge(existing, {}, prior_record)

    assert merged == {"permissions": {"allow": ["Bash(ls:*)"]}}
    assert record == {"leaves": [], "lists": {}}


@pytest.mark.feature("claude-settings-additive-merge")
def test_remove_recorded_settings_tolerates_a_record_without_leaves_or_lists():
    from scripts.targets.claude_target import _remove_recorded_settings

    doc = {"env": {"KEPT": "1"}}

    _remove_recorded_settings(doc, {})

    assert doc == {"env": {"KEPT": "1"}}


@pytest.mark.feature("claude-settings-additive-merge")
def test_remove_recorded_settings_skips_a_list_entry_whose_doc_value_is_not_a_list():
    from scripts.targets.claude_target import _remove_recorded_settings

    doc = {"a": {"b": "not-a-list"}}

    _remove_recorded_settings(doc, {"leaves": [], "lists": {"a.b": ["x"]}})

    assert doc == {"a": {"b": "not-a-list"}}


@pytest.mark.feature("claude-settings-additive-merge")
def test_remove_recorded_settings_removes_listed_entries_from_a_three_level_list():
    from scripts.targets.claude_target import _remove_recorded_settings

    doc = {"a": {"b": {"allow": ["keep", "drop"]}}}

    _remove_recorded_settings(doc, {"leaves": [], "lists": {"a.b.allow": ["drop"]}})

    assert doc == {"a": {"b": {"allow": ["keep"]}}}


@pytest.mark.feature("claude-settings-additive-merge")
def test_remove_owned_hooks_tolerates_a_group_without_a_hooks_list():
    from scripts.targets.claude_target import _remove_owned_hooks

    doc = {"hooks": {"PreToolUse": [{"notHooks": []}]}}

    _remove_owned_hooks(doc, {"python -m hooks"})

    assert doc == {"hooks": {"PreToolUse": [{"notHooks": []}]}}


@pytest.mark.feature("claude-settings-additive-merge")
def test_remove_owned_hooks_drops_the_hooks_key_when_every_group_was_ours():
    from scripts.targets.claude_target import _remove_owned_hooks

    doc = {"hooks": {"PreToolUse": [{"hooks": [{"command": "python -m hooks"}]}]}, "other": 1}

    _remove_owned_hooks(doc, {"python -m hooks"})

    assert doc == {"other": 1}


@pytest.mark.feature("claude-settings-additive-merge")
def test_remove_owned_hooks_keeps_checking_events_after_a_malformed_one():
    from scripts.targets.claude_target import _remove_owned_hooks

    doc = {
        "hooks": {
            "EventA": "not-a-list",
            "EventB": [{"hooks": [{"command": "python -m hooks"}]}],
        }
    }

    _remove_owned_hooks(doc, {"python -m hooks"})

    assert doc == {"hooks": {"EventA": "not-a-list"}}


@pytest.mark.feature("claude-settings-additive-merge")
def test_remove_owned_plugins_tolerates_a_name_the_operator_already_removed():
    from scripts.targets.claude_target import _remove_owned_plugins

    doc = {"enabledPlugins": {"mine@m": True}}

    _remove_owned_plugins(doc, {"gone@m"})

    assert doc == {"enabledPlugins": {"mine@m": True}}


@pytest.mark.feature("claude-settings-additive-merge")
def test_remove_managed_keys_returns_false_for_a_missing_or_unmanaged_or_invalid_file(tmp_path):
    from scripts.targets.claude_target import remove_managed_keys

    missing = tmp_path / "missing.json"
    assert remove_managed_keys(missing) is False

    unmanaged = tmp_path / "unmanaged.json"
    unmanaged.write_text(json.dumps({"model": "opus"}))
    assert remove_managed_keys(unmanaged) is False

    invalid = tmp_path / "invalid.json"
    invalid.write_text("not json")
    assert remove_managed_keys(invalid) is False


@pytest.mark.feature("claude-settings-additive-merge")
def test_remove_managed_keys_returns_true_when_operator_content_remains():
    from scripts.targets.claude_target import ClaudeAdapter, remove_managed_keys

    path = ClaudeAdapter().write_settings({"env": {"AGENTIHOOKS_VAR": "1"}})
    doc = json.loads(path.read_text())
    doc["operatorKey"] = "kept"
    path.write_text(json.dumps(doc))

    assert remove_managed_keys(path) is True
    assert json.loads(path.read_text()) == {"operatorKey": "kept"}


@pytest.mark.feature("claude-settings-additive-merge")
def test_remove_managed_keys_tolerates_state_with_no_recorded_keys_at_all():
    from scripts.targets.claude_target import ClaudeAdapter, remove_managed_keys

    path = ClaudeAdapter().write_settings({"env": {"AGENTIHOOKS_VAR": "1"}})
    doc = json.loads(path.read_text())
    doc["operatorKey"] = "kept"
    path.write_text(json.dumps(doc))
    install._save_state({})

    assert remove_managed_keys(path) is True
    assert json.loads(path.read_text()) == {"env": {"AGENTIHOOKS_VAR": "1"}, "operatorKey": "kept"}


@pytest.mark.feature("claude-settings-additive-merge")
def test_remove_managed_keys_returns_false_and_deletes_the_file_when_nothing_operator_owned_remains():
    from scripts.targets.claude_target import ClaudeAdapter, remove_managed_keys

    path = ClaudeAdapter().write_settings({"env": {"AGENTIHOOKS_VAR": "1"}})

    assert remove_managed_keys(path) is False
    assert not path.exists()


@pytest.mark.feature("claude-settings-additive-merge")
def test_enabled_plugins_reads_bundle_user_and_shared_separately(tmp_path):
    from scripts.targets.claude_target import enabled_plugins

    plugin = {"kind": "claude-plugin", "check": ["true"], "install": ["true"]}
    bundle, user, shared = tmp_path / "bundle", tmp_path / "user", tmp_path / "shared"
    for root, name in ((bundle, "bundle@m"), (user, "user@m"), (shared, "shared@m")):
        root.mkdir()
        (root / "deps.json").write_text(json.dumps({"deps": [{**plugin, "id": name}]}))

    result = enabled_plugins({}, {}, bundle, user, shared)

    assert result == {"bundle@m": True, "user@m": True, "shared@m": True}


@pytest.mark.feature("claude-settings-additive-merge")
def test_enabled_plugins_records_the_union_of_owned_and_layered_names(tmp_path):
    from scripts.targets.claude_target import PLUGIN_RECORD_KEY, enabled_plugins

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    plugin = {"kind": "claude-plugin", "check": ["true"], "install": ["true"]}
    (bundle / "deps.json").write_text(json.dumps({"deps": [{**plugin, "id": "fleet@m"}]}))

    enabled_plugins({}, {"layer@m": True}, bundle)

    assert install._load_state()[PLUGIN_RECORD_KEY] == ["fleet@m", "layer@m"]


@pytest.mark.feature("claude-settings-additive-merge")
def test_write_settings_keeps_hooks_and_enabled_plugins_out_of_the_generic_merge():
    from scripts.targets.claude_target import SETTINGS_RECORD_KEY, ClaudeAdapter

    rendered = {
        "hooks": {"PreToolUse": [{"hooks": [{"command": "python -m hooks"}]}]},
        "enabledPlugins": {"fleet@m": True},
    }
    path = ClaudeAdapter().write_settings(rendered)
    written = json.loads(path.read_text())

    assert written["hooks"] == rendered["hooks"]
    assert written["enabledPlugins"] == {"fleet@m": True}
    record = install._load_state()[SETTINGS_RECORD_KEY]
    assert not any(leaf.startswith(("hooks.", "enabledPlugins.")) for leaf in record["leaves"])
    assert not any(key.startswith(("hooks.", "enabledPlugins.")) for key in record["lists"])


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
