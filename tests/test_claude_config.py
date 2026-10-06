import json
import os
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("environ", "suffix"),
    [
        ({}, ".claude"),
        ({"AGENTIHOOKS_CLAUDE_HOME": "~/legacy"}, "legacy"),
        ({"CLAUDE_CODE_HOME_DIR": "~/code", "AGENTIHOOKS_CLAUDE_HOME": "~/legacy"}, "code/.claude"),
        (
            {"CLAUDE_CONFIG_DIR": "~/role", "CLAUDE_CODE_HOME_DIR": "~/code", "AGENTIHOOKS_CLAUDE_HOME": "~/legacy"},
            "role",
        ),
        ({"CLAUDE_CONFIG_DIR": "", "CLAUDE_CODE_HOME_DIR": "", "AGENTIHOOKS_CLAUDE_HOME": ""}, ".claude"),
    ],
)
def test_home_precedence(environ, suffix):
    from scripts.claude_config import claude_home

    assert claude_home(environ) == Path.home() / suffix


@pytest.mark.parametrize(
    ("environ", "suffix"),
    [
        ({}, ".claude.json"),
        ({"HOME": "~/alternate"}, "alternate/.claude.json"),
        ({"AGENTIHOOKS_CLAUDE_HOME": "~/legacy"}, ".claude.json"),
        ({"CLAUDE_CODE_HOME_DIR": "~/code"}, "code/.claude.json"),
        ({"CLAUDE_CONFIG_DIR": "~/role", "CLAUDE_CODE_HOME_DIR": "~/code"}, "role/.claude.json"),
    ],
)
def test_json_precedence(environ, suffix):
    from scripts.claude_config import claude_json

    assert claude_json(environ) == Path.home() / suffix


def test_config_readers(tmp_path, monkeypatch):
    import install

    from scripts import claude_quota_balancer, claude_trust, mcp_daemon, mcp_reporter, status_checker

    home = tmp_path / "role"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")
    monkeypatch.chdir(tmp_path)
    entry = {"command": "role-server"}
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"agentihooks": entry}}))
    (home / "settings.json").write_text(json.dumps({"model": "fable"}))
    assert install._resolve_claude_home() == home
    assert install._resolve_claude_json() == home / ".claude.json"
    assert claude_trust._config_path(dict(os.environ)) == home / ".claude.json"
    assert claude_quota_balancer.requested_model([]) == "fable"
    assert mcp_reporter.load_all_mcp_configs()["agentihooks"]["config"] == entry
    assert mcp_daemon._client_entry() == entry
    monkeypatch.setattr(status_checker, "_count_hooks_utils_tools", lambda: 1)
    monkeypatch.setattr(status_checker, "_save_tool_cache", lambda data: None)
    assert status_checker.check_mcp()["servers"]["agentihooks"]["tools"] == 1


def test_model_explicit_path_and_flags(tmp_path, monkeypatch):
    from scripts.claude_quota_balancer import requested_model

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "missing"))
    path = tmp_path / "explicit.json"
    path.write_text(json.dumps({"model": "explicit"}))
    assert requested_model([], path) == "explicit"
    assert requested_model(["--model", "flag"], path) == "flag"
    assert requested_model(["--model=flag"], path) == "flag"


def test_profile_detection_and_project_data(tmp_path, monkeypatch):
    import install

    from hooks.context import project_bridge, project_sessions
    from hooks.context.broadcast import encode_cwd

    home = tmp_path / "role"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    source = tmp_path / "profiles" / "engineer" / ".claude" / "CLAUDE.md"
    source.parent.mkdir(parents=True)
    source.write_text("engineer")
    (home / "CLAUDE.md").symlink_to(source)
    assert install._detect_active_profile() == "engineer"
    root = Path.home() / "project"
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.setattr(
        "hooks.context.project_identity._git",
        lambda cwd, *args: (
            str(root / ".git") if "--git-common-dir" in args else str(root) if "--show-toplevel" in args else ""
        ),
    )
    folder = home / "projects" / encode_cwd(str(root))
    (folder / "memory").mkdir(parents=True)
    memory = folder / "memory" / "MEMORY.md"
    memory.write_text("Role memory")
    (folder / "role-session.jsonl").write_text("{}\n")
    assert project_bridge._memory_file(root) == memory
    assert project_sessions.lookup("role-session").cwd == str(root)
    (home / "rules").mkdir()
    (home / "rules" / "role.md").write_text("Role rule")
    from argparse import Namespace

    monkeypatch.setattr("hooks.context.rules_refresh._collect_pending_sessions", lambda: [])
    install._cmd_refresh_rules(Namespace(profile=None, clear=False, dry_run=True))


@pytest.mark.parametrize("override", ["engineer", "engineer,brain"])
def test_profile_override(override, monkeypatch):
    from hooks.context.profile_chain import active_profile

    monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")

    monkeypatch.setenv("AGENTIHOOKS_PROFILE", override)
    assert active_profile({"targets": {"global": {"claude": {"profile": "anton"}}}}) == override
    assert active_profile({}) == override


def test_empty_profile_uses_state(monkeypatch):
    from hooks.context.profile_chain import active_profile

    monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")

    monkeypatch.setenv("AGENTIHOOKS_PROFILE", "")
    assert active_profile({"targets": {"global": {"claude": {"profile": "anton"}}}}) == "anton"
    assert active_profile({}) is None


def test_profile_trace_and_consumers(tmp_path, monkeypatch):
    from hooks.context import enforcement, injection_trace, profile_chain
    from hooks.lifecycle.config import _overlay_files

    bundle = tmp_path / "bundle"
    role = bundle / "profiles" / "engineer"
    role.mkdir(parents=True)
    (role / "profile.yml").write_text("name: engineer\n")
    (role / "enforcements.json").write_text(
        json.dumps({"enforcements": [{"id": "role-proof", "message": "Engineer selected", "every_n_tools": 1}]})
    )
    monkeypatch.setattr(
        profile_chain,
        "read_state",
        lambda: {
            "targets": {"global": {"claude": {"targets": {"global": {"claude": {"profile": "anton"}}}}}},
            "bundle": {"path": str(bundle)},
        },
    )
    monkeypatch.setenv("AGENTIHOOKS_PROFILE", "engineer")
    entries = enforcement._load_profile_enforcements()
    assert [entry["id"] for entry in entries] == ["role-proof"]
    assert role / "lifecycle.json" in _overlay_files()
    injection_trace.record_enforcements("role-session", entries)
    row = injection_trace.trace("role-session")[0]
    assert row["profile"] == "engineer"
    assert row["layer"] == "profile"
    assert row["locator"]["store"] == str(role / "enforcements.json")


def test_resolver_is_isolated(monkeypatch):
    from hooks.context.profile_chain import active_profile
    from scripts.claude_config import claude_home, claude_json

    monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")

    assert claude_home() == Path.home() / ".claude"
    assert claude_json() == Path.home() / ".claude.json"
    assert active_profile({}) is None
