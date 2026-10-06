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
