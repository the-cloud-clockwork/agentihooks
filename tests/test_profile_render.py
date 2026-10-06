import hashlib
import json
import os
import re
import subprocess
import tomllib
from pathlib import Path

import pytest

SHARED = {"projects", "sessions", "todos", "plugins", ".credentials.json"}


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _git(bundle: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t"}
    env["GIT_COMMITTER_EMAIL"] = "t@t"
    return subprocess.run(["git", *args], cwd=bundle, env=env, check=True, capture_output=True, text=True).stdout


def _commit(bundle: Path, message: str) -> None:
    _git(bundle, "add", "-A")
    _git(bundle, "commit", "-q", "--allow-empty", "-m", message)


def _flat(text: str) -> str:
    return " ".join(text.split())


@pytest.fixture
def world(tmp_path, monkeypatch):
    from scripts.targets._common import _install_module

    install = _install_module()
    python = tmp_path / "venv" / "bin" / "python"
    monkeypatch.setattr(install, "_detect_venv", lambda: python)
    home = Path.home()
    bundle = tmp_path / "bundle"
    _write(bundle / ".claude" / "CLAUDE.md", "BUNDLE DIRECTIVE MARKER\n")
    _write(bundle / ".claude" / "skills" / "bundle-skill" / "SKILL.md", "---\nname: bundle-skill\n---\n")
    _write(bundle / ".claude" / "rules" / "bundle-rule.md", "BUNDLE RULE MARKER\n")
    _write(bundle / ".claude" / "settings.overrides.json", json.dumps({"env": {"BUNDLE_FLAG": "1"}}))
    _write(bundle / ".claude" / ".mcp.json", json.dumps({"mcpServers": {"bundle-srv": {"command": "bundle-server"}}}))
    _write(bundle / ".codex" / "config.overrides.toml", 'approval_policy = "never"\n')
    profiles = bundle / "profiles"
    _write(profiles / "rb-base" / "profile.yml", "name: rb-base\n")
    _write(profiles / "rb-base" / "CLAUDE.md", "BASE PERSONA MARKER\n")
    _write(profiles / "rb-base" / ".codex" / "config.overrides.toml", 'sandbox_mode = "workspace-write"\n')
    _write(profiles / "rb-kit" / "profile.yml", "name: rb-kit\n")
    _write(profiles / "rb-kit" / ".claude" / "skills" / "role-skill" / "SKILL.md", "---\nname: role-skill\n---\n")
    _write(profiles / "rb-kit" / ".claude" / "rules" / "role-rule.md", "ROLE RULE MARKER\n")
    _write(profiles / "rb-kit" / ".claude" / "rules" / "README.md", "KIT README\n")
    _write(profiles / "rb-kit" / ".claude" / "rules" / "notes.txt", "KIT NOTES\n")
    _write(profiles / "rb-role" / "profile.yml", "name: rb-role\nextends: [rb-base, rb-kit]\n")
    _write(profiles / "rb-role" / "CLAUDE.md", "ROLE PERSONA MARKER\n")
    _write(profiles / "rb-role" / "hooks" / "role.sh", "true\n")
    hooks = {"Stop": [{"hooks": [{"type": "command", "command": "bash hooks/role.sh"}]}]}
    _write(profiles / "rb-role" / ".claude" / "settings.overrides.json", json.dumps({"hooks": hooks}))
    servers = {
        "role-srv": {"command": "role-server"},
        "leaky-arg": {"command": "srv", "args": ["--token", "ghp_" + "a" * 36]},
        "leaky-env": {"command": "srv", "env": {"GH_TOKEN": "ghp_" + "b" * 36, "SAFE": "${REF}"}},
    }
    _write(profiles / "rb-role" / ".claude" / ".mcp.json", json.dumps({"mcpServers": servers}))
    _write(profiles / "rb-other" / "profile.yml", "name: rb-other\n")
    _write(profiles / "rb-other" / ".claude" / "skills" / "other-skill" / "SKILL.md", "---\nname: other-skill\n---\n")
    _git(bundle, "init", "-q")
    _commit(bundle, "one")
    install._save_state({"bundle": {"path": str(bundle)}})

    claude = home / ".claude"
    _write(claude / "settings.json", json.dumps({"model": "opus", "theme": "dark"}))
    (claude / "projects").mkdir()
    gmail = {"type": "http", "url": "https://gmail.example"}
    claude_json = {"hasCompletedOnboarding": True, "mcpServers": {"google-gmail": gmail}, "projects": {"/w": {}}}
    _write(home / ".claude.json", json.dumps(claude_json))
    codex_mcp = "[mcp_servers.google-gmail]\nurl = 'https://gmail.example'\n\n[mcp_servers.role-srv]\ncommand = 'r'\n"
    codex_mcp += "\n[mcp_servers.bundle-srv]\ncommand = 'b'\n"
    _write(home / ".codex" / "config.toml", codex_mcp)
    skills = home / ".agents" / "skills"
    for name in ("role-skill", "other-skill", "bundle-skill"):
        (skills / name).mkdir()
    _write(skills / "stray.md", "not a skill\n")
    return {"home": home, "bundle": bundle, "install": install, "python": python, "role": profiles / "rb-role"}


def _tree_hashes(root: Path, skip: Path) -> dict[str, str]:
    out = {}
    for path in sorted(root.rglob("*")):
        if path == skip or skip in path.parents or not path.is_file() or path.is_symlink():
            continue
        out[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def test_claude_render_tree(world, capsys):
    from scripts.profiles import render

    home, install = world["home"], world["install"]
    out = render.render_claude("rb-role")

    assert out == home / ".agentihooks" / "profiles" / "rb-role" / "claude"
    package_skills = {p.name for p in (install.PACKAGE_FEATURES_DIR / "skills").iterdir() if p.is_dir()}
    skills = {p.name for p in (out / "skills").iterdir()}
    assert skills == package_skills | {"role-skill", "bundle-skill"}
    assert all((out / "skills" / name).is_symlink() for name in skills)
    assert not (out / "rules").exists()
    persona = (out / "CLAUDE.md").read_text()
    for marker in ("BUNDLE DIRECTIVE MARKER", "BASE PERSONA MARKER", "ROLE PERSONA MARKER"):
        assert marker in persona
    assert "KIT README" not in persona and "KIT NOTES" not in persona
    assert persona.startswith(render.HEADER)
    assert persona.endswith(f"\n\n{render.FOOTER}\n")


def test_claude_render_folds_every_rule_into_claude_md(world):
    from scripts.profiles import render, sources

    out = render.render_claude("rb-role")

    persona = (out / "CLAUDE.md").read_text()
    assert "<!-- rule: bundle-rule.md (rule) -->\nBUNDLE RULE MARKER" in persona
    assert "<!-- rule: role-rule.md (rule) -->\nROLE RULE MARKER" in persona
    assert persona.index("ROLE PERSONA MARKER") < persona.index("ROLE RULE MARKER")
    assert not (out / "rules").exists()
    rows = json.loads(sources.path("rb-role", "claude", render.rendered_root()).read_text())
    ruled = {Path(row["locator"]["path"]).name for row in rows if row["layer"] == "rule"}
    assert {"bundle-rule.md", "role-rule.md"} <= ruled


def test_claude_render_drops_the_rules_folder_of_an_earlier_render(world):
    from scripts.profiles import render

    out = render.render_claude("rb-role")
    _write(out / "rules" / "role-rule.md", "OLD COPIED RULE\n")
    (out / "rules" / "linked.md").symlink_to(world["bundle"] / ".claude" / "rules" / "bundle-rule.md")
    (out / render.STAMP).write_text(json.dumps(render.stamp("rb-role")))

    assert render.render_claude("rb-role") == out
    assert not (out / "rules").exists()
    assert (world["bundle"] / ".claude" / "rules" / "bundle-rule.md").read_text() == "BUNDLE RULE MARKER\n"
    assert render.render_claude("rb-role") is None


def test_claude_render_refreshes_folded_rules(world):
    from scripts.profiles import render

    out = render.render_claude("rb-role")
    source = world["bundle"] / ".claude" / "rules" / "bundle-rule.md"
    _write(source, "---\npaths: ['**/*.py']\n---\nUPDATED BUNDLE RULE\n")

    assert render.render_claude("rb-role", force=True) == out
    persona = (out / "CLAUDE.md").read_text()
    assert "UPDATED BUNDLE RULE" in persona
    assert "BUNDLE RULE MARKER" not in persona


def test_refresh_rules_updates_the_rendered_claude_md(world, monkeypatch):
    from scripts.profiles import render
    from scripts.targets.claude_target import refresh_rules

    out = render.render_claude("rb-role")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(out))
    source = world["bundle"] / ".claude" / "rules" / "bundle-rule.md"
    _write(source, "UPDATED RENDERED RULE\n")
    _write(out / "CLAUDE.local.md", "ROLE LOCAL OVERRIDE\n")

    payload = refresh_rules(out / "rules", out / "CLAUDE.md", out / "CLAUDE.local.md", False)

    assert "UPDATED RENDERED RULE" in (out / "CLAUDE.md").read_text()
    assert not (out / "rules").exists()
    assert "UPDATED RENDERED RULE" in payload
    assert "ROLE PERSONA MARKER" in payload
    assert "ROLE LOCAL OVERRIDE" in payload


def test_claude_md_sanity_still_caps_the_rendered_claude_md(world, monkeypatch):
    from hooks.context import claude_md_sanity
    from hooks.hook_manager import BlockAction
    from scripts.profiles import render

    out = render.render_claude("rb-role")
    monkeypatch.setattr(claude_md_sanity, "get_max_lines", lambda session_id: 3)
    edit = {"old_string": "ROLE RULE MARKER", "new_string": "ROLE RULE MARKER\nMORE"}
    payload = {"tool_name": "Edit", "tool_input": {"file_path": str(out / "CLAUDE.md"), **edit}}

    with pytest.raises(BlockAction, match="CLAUDE.md cap of 3 lines"):
        claude_md_sanity.check_claude_md_write(payload)


def test_claude_render_settings(world):
    from scripts.profiles import render

    out = render.render_claude("rb-role")

    text = (out / "settings.json").read_text()
    assert "__PYTHON__" not in text
    assert str(world["python"]) in text
    settings = json.loads(text)
    assert settings["model"] == "opus"
    assert "theme" not in settings
    assert settings["env"]["BUNDLE_FLAG"] == "1"
    resolved = str((world["role"] / "hooks" / "role.sh").resolve())
    assert {"type": "command", "command": f"bash {resolved}"} in settings["hooks"]["Stop"][-1]["hooks"]


def test_claude_render_enables_only_the_chain_plugins(world):
    from scripts.profiles import render

    home, bundle = world["home"], world["bundle"]
    operator = {"model": "opus", "enabledPlugins": {"mine@m": True, "kit@m": False}}
    _write(home / ".claude" / "settings.json", json.dumps(operator))
    plugin = {"kind": "claude-plugin", "check": ["true"], "install": ["true"]}
    _write(bundle / "deps.json", json.dumps({"deps": [{**plugin, "id": "fleet@m"}]}))
    kit = {"enabledPlugins": {"kit@m": True, "muted@m": False}}
    _write(bundle / "profiles" / "rb-kit" / ".claude" / "settings.overrides.json", json.dumps(kit))

    out = render.render_claude("rb-role")

    assert json.loads((out / "settings.json").read_text())["enabledPlugins"] == {"kit@m": True}
    assert json.loads((home / ".claude" / "settings.json").read_text()) == operator


MATTPOCOCK, PLAYWRIGHT = "mattpocock-skills@claude-plugins-official", "playwright@claude-plugins-official"


@pytest.mark.parametrize(
    ("role", "plugins"),
    [("engineer", [MATTPOCOCK]), ("cicd", [MATTPOCOCK]), ("planner", [MATTPOCOCK]), ("master", [PLAYWRIGHT])],
)
def test_claude_render_enables_the_role_defaults(world, role, plugins):
    from scripts.profiles import render

    _write(world["home"] / ".claude" / "settings.json", json.dumps({"enabledPlugins": {"mine@m": True}}))
    _write(world["bundle"] / "profiles" / role / "profile.yml", f"name: {role}\nextends: [rb-base]\n")

    out = render.render_claude(role)

    assert json.loads((out / "settings.json").read_text())["enabledPlugins"] == dict.fromkeys(plugins, True)


@pytest.mark.parametrize(
    ("layer", "plugins"),
    [
        ({"impeccable@impeccable": True}, {MATTPOCOCK: True, "impeccable@impeccable": True}),
        ({MATTPOCOCK: False, "own@m": True}, {"own@m": True}),
    ],
)
def test_a_same_name_profile_extends_or_replaces_the_role_defaults(world, layer, plugins):
    from scripts.profiles import render

    engineer = world["bundle"] / "profiles" / "engineer"
    _write(engineer / "profile.yml", "name: engineer\n")
    _write(engineer / ".claude" / "settings.overrides.json", json.dumps({"enabledPlugins": layer}))

    out = render.render_claude("engineer")

    assert json.loads((out / "settings.json").read_text())["enabledPlugins"] == plugins


def test_a_profile_extending_a_role_keeps_its_defaults(world):
    from scripts.profiles import render

    profiles = world["bundle"] / "profiles"
    _write(profiles / "engineer" / "profile.yml", "name: engineer\n")
    _write(profiles / "rb-front" / "profile.yml", "name: rb-front\nextends: [engineer]\n")
    front = {"enabledPlugins": {"frontend-design@claude-plugins-official": True}}
    _write(profiles / "rb-front" / ".claude" / "settings.overrides.json", json.dumps(front))

    out = render.render_claude("rb-front")

    assert json.loads((out / "settings.json").read_text())["enabledPlugins"] == {
        MATTPOCOCK: True,
        "frontend-design@claude-plugins-official": True,
    }


def test_the_package_prefix_names_the_same_role(world, tmp_path, monkeypatch):
    from hooks.context import profile_chain
    from scripts.profiles import render

    roles = tmp_path / "package-roles"
    monkeypatch.setattr(profile_chain, "PACKAGE_ROLES", roles)
    _write(roles / "master" / "profile.yml", "name: master\n")
    world["install"]._save_state({})

    out = render.render_claude("package:master")

    assert json.loads((out / "settings.json").read_text())["enabledPlugins"] == {PLAYWRIGHT: True}


def test_claude_render_subscribes_every_profile_to_brain(world):
    from scripts.profiles import render

    out = render.render_claude("rb-role")

    assert json.loads((out / "settings.json").read_text())["env"]["AGENTIHOOKS_BASE_CHANNELS"] == "brain"
    assert render.channels("rb-role") == "brain"


def test_brain_joins_the_profile_channels_once(world):
    from scripts.profiles import render

    overrides = world["role"] / ".claude" / "settings.overrides.json"
    settings = json.loads(overrides.read_text())
    settings["env"] = {"AGENTIHOOKS_BASE_CHANNELS": "amygdala, ops"}
    overrides.write_text(json.dumps(settings))
    assert render.channels("rb-role") == "amygdala,ops,brain"

    settings["env"] = {"AGENTIHOOKS_BASE_CHANNELS": "brain,amygdala"}
    overrides.write_text(json.dumps(settings))
    out = render.render_claude("rb-role", force=True)
    assert json.loads((out / "settings.json").read_text())["env"]["AGENTIHOOKS_BASE_CHANNELS"] == "brain,amygdala"
    assert render.channels("rb-role") == "brain,amygdala"


def test_channels_read_the_bundle_layer(world):
    from scripts.profiles import render

    overrides = {"env": {"AGENTIHOOKS_BASE_CHANNELS": "amygdala"}}
    (world["bundle"] / ".claude" / "settings.overrides.json").write_text(json.dumps(overrides))

    assert render.channels("rb-role") == "amygdala,brain"


def test_claude_render_excludes_default_home_instructions(world):
    from scripts.profiles import render

    out = render.render_claude("rb-role")

    excludes = json.loads((out / "settings.json").read_text())["claudeMdExcludes"]
    default_home = world["home"] / ".claude"
    assert excludes == [str(default_home / "CLAUDE.md"), str(default_home / "rules" / "**")]


def test_claude_render_mcp_and_shared_data(world, capsys):
    from scripts.profiles import render

    home, install = world["home"], world["install"]
    out = render.render_claude("rb-role")

    claude_json = json.loads((out / ".claude.json").read_text())
    agentihooks = install._build_mcp_config("all")["mcpServers"]["agentihooks"]
    assert claude_json["mcpServers"] == {
        "agentihooks": agentihooks,
        "bundle-srv": {"command": "bundle-server"},
        "role-srv": {"command": "role-server"},
        "leaky-env": {"command": "srv", "env": {"SAFE": "${REF}"}},
    }
    printed = capsys.readouterr().out
    assert "MCP 'leaky-arg' carries credential-shaped literals in args[1]" in printed
    assert "MCP 'leaky-env' env var 'GH_TOKEN' looks like a credential" in printed
    assert printed.count(f"from {out / '.claude.json'}") == 1
    assert printed.count(f"written to {out / '.claude.json'}") == 1
    assert claude_json["hasCompletedOnboarding"] is True
    assert claude_json["projects"] == {"/w": {}}
    links = {p.name for p in out.iterdir() if p.is_symlink()}
    assert links == SHARED
    for name in SHARED:
        assert os.readlink(out / name) == str(home / ".claude" / name)


def test_claude_render_seeds_only_startup_state_from_the_default_home(world):
    from scripts.profiles import render

    project = {"hasTrustDialogAccepted": True, "mcpServers": {"own": {"command": "x"}}, "enabledMcpjsonServers": ["a"]}
    operator = {
        "hasCompletedOnboarding": True,
        "userID": "u1",
        "numStartups": 40,
        "claudeAiMcpEverConnected": True,
        "mcpServers": {"google-gmail": {"type": "http", "url": "https://gmail.example"}},
        "projects": {"/w": project},
    }
    _write(world["home"] / ".claude.json", json.dumps(operator))

    out = render.render_claude("rb-role")

    claude_json = json.loads((out / ".claude.json").read_text())
    assert {k: v for k, v in claude_json.items() if k != "mcpServers"} == {
        "hasCompletedOnboarding": True,
        "userID": "u1",
        "projects": {"/w": {"hasTrustDialogAccepted": True}},
    }
    assert "google-gmail" not in claude_json["mcpServers"]


@pytest.mark.parametrize("operator", [None, "not json", json.dumps({"projects": ["/w"], "userID": "u1"})])
def test_claude_render_seeds_nothing_it_cannot_read(world, operator):
    from scripts.profiles import render

    path = world["home"] / ".claude.json"
    if operator is None:
        path.unlink()
    else:
        path.write_text(operator)

    out = render.render_claude("rb-role")

    claude_json = json.loads((out / ".claude.json").read_text())
    assert set(claude_json) == ({"mcpServers", "userID"} if operator and "u1" in operator else {"mcpServers"})


def test_claude_render_gives_each_home_its_own_plans_folder(world):
    from scripts.profiles import render

    out = render.render_claude("rb-role")
    assert (out / "plans").is_dir() and not (out / "plans").is_symlink()

    (out / "plans").rmdir()
    (out / "plans").symlink_to(world["home"] / ".claude" / "plans")
    render.render_claude("rb-role", force=True)
    assert (out / "plans").is_dir() and not (out / "plans").is_symlink()


def test_claude_render_from_inside_a_rendered_home(world, monkeypatch):
    from scripts.profiles import render

    home = world["home"]
    elsewhere = home / "elsewhere"
    _write(elsewhere / "settings.json", json.dumps({"model": "haiku"}))
    _write(elsewhere / ".claude.json", json.dumps({"hasCompletedOnboarding": False}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(elsewhere))

    out = render.render_claude("rb-role")

    assert json.loads((out / "settings.json").read_text())["model"] == "opus"
    assert json.loads((out / ".claude.json").read_text())["hasCompletedOnboarding"] is True
    assert os.readlink(out / "projects") == str(home / ".claude" / "projects")


def test_claude_render_keeps_runtime_state_of_previous_render(world):
    from scripts.profiles import render

    out = render.render_claude("rb-role")
    claude_json = json.loads((out / ".claude.json").read_text())
    claude_json["numStartups"] = 7
    claude_json["mcpServers"]["stale"] = {"command": "x"}
    (out / ".claude.json").write_text(json.dumps(claude_json))
    (out / "skills" / "gone").symlink_to(out)

    assert render.render_claude("rb-role") is None
    assert render.render_claude("rb-role", force=True) == out

    claude_json = json.loads((out / ".claude.json").read_text())
    assert claude_json["numStartups"] == 7
    assert "stale" not in claude_json["mcpServers"]
    assert not (out / "skills" / "gone").is_symlink()


def test_codex_render_writes_one_layered_profile(world):
    from scripts.profiles import render

    home = world["home"]
    before = _tree_hashes(home, home / ".agentihooks" / "profiles")

    path = render.render_codex("rb-role")

    assert path == home / ".codex" / "rb-role.config.toml"
    after = _tree_hashes(home, home / ".agentihooks" / "profiles")
    assert set(after) - set(before) == {".codex/rb-role.config.toml"}
    assert {k: v for k, v in after.items() if k in before} == before
    doc = tomllib.loads(path.read_text())
    instructions = doc["developer_instructions"]
    for marker in ("BUNDLE DIRECTIVE MARKER", "BASE PERSONA MARKER", "ROLE RULE MARKER", "BUNDLE RULE MARKER"):
        assert marker in instructions
    assert "<!-- rule: role-rule.md (rule) -->" in instructions
    assert instructions.endswith(f"\n\n{render.FOOTER}\n")
    assert "KIT README" not in instructions
    assert doc["sandbox_mode"] == "workspace-write"
    assert doc["approval_policy"] == "never"
    assert "model_context_window" not in doc
    assert doc["mcp_servers"] == {"google-gmail": {"enabled": False}}
    other = str(home / ".agents" / "skills" / "other-skill" / "SKILL.md")
    assert doc["skills"]["config"] == [{"path": other, "enabled": False}]
    assert doc["agentihooks"]["render"] == render.stamp("rb-role")
    assert render.render_codex("rb-role") is None


@pytest.mark.parametrize("config", [None, 'model = "gpt"\n'])
def test_codex_render_without_global_servers_or_skills(world, config):
    from scripts.profiles import render

    home = world["home"]
    (home / ".codex" / "config.toml").unlink()
    if config:
        _write(home / ".codex" / "config.toml", config)
    for skill in (home / ".agents" / "skills").iterdir():
        skill.unlink() if skill.is_file() else skill.rmdir()

    doc = tomllib.loads(render.render_codex("rb-role").read_text())

    assert "mcp_servers" not in doc
    assert "skills" not in doc


def test_codex_render_replaces_an_unstamped_profile_file(world):
    from scripts.profiles import render

    path = _write(world["home"] / ".codex" / "rb-role.config.toml", 'model = "hand-written"\n')

    assert render.render_codex("rb-role") == path
    assert "hand-written" not in path.read_text()


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_render_leaves_global_install_untouched(world, target):
    from scripts.profiles import render

    home = world["home"]
    skip = home / ".agentihooks" / "profiles"
    before = _tree_hashes(home, skip)

    render.render(target, "rb-role")

    after = _tree_hashes(home, skip)
    if target == "codex":
        after.pop(".codex/rb-role.config.toml")
    assert after == before


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_stamp_skips_fresh_render_and_redoes_stale(world, target):
    from scripts.profiles import render

    bundle = world["bundle"]
    assert render.render(target, "rb-role") is not None
    assert render.render(target, "rb-role") is None
    assert render.render(target, "rb-role", force=True) is not None

    _commit(bundle, "two")
    assert render.render(target, "rb-role") is not None
    assert render.render(target, "rb-role") is None

    _write(bundle / "profiles" / "rb-role" / "profile.yml", "name: rb-role\nextends: [rb-base, rb-kit, rb-other]\n")
    assert render.render(target, "rb-role") is not None


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_stamp_ignores_operator_plugins(world, target):
    from scripts.profiles import render

    settings = world["home"] / ".claude" / "settings.json"
    _write(settings, json.dumps({"enabledPlugins": {"mine@m": True}}))
    assert render.render(target, "rb-role") is not None

    _write(settings, json.dumps({"enabledPlugins": {"mine@m": True, "later@m": True}}))
    assert render.render(target, "rb-role") is None


def test_stamp_names_the_chain_role_defaults(world, monkeypatch):
    from scripts.profiles import plugins, render

    _write(world["bundle"] / "profiles" / "master" / "profile.yml", "name: master\nextends: [rb-role]\n")
    assert render.stamp("rb-role")["plugins"] == {}
    assert render.stamp("master")["plugins"] == {PLAYWRIGHT: True}
    assert render.render("claude", "master") is not None

    monkeypatch.setitem(plugins.ROLE_PLUGINS, "master", ("other@m",))
    assert render.stamp("master")["plugins"] == {"other@m": True}
    assert render.render("claude", "master") is not None


def test_rendered_profiles_lists_the_homes_each_target_has(world):
    from scripts.profiles import render

    assert render.rendered_profiles("claude") == []
    render.render_claude("rb-role")
    render.render_claude("rb-other")
    render.render_codex("rb-other")
    _write(world["home"] / ".codex" / "hand.config.toml", 'model = "x"\n')

    assert render.rendered_profiles("claude") == ["rb-other", "rb-role"]
    assert render.rendered_profiles("codex") == ["rb-other"]
    assert render.rendered_profiles("copilot") == []


def test_init_rerenders_every_existing_profile_home(world, monkeypatch, capsys):
    from argparse import Namespace

    from scripts.profiles import render

    install = world["install"]
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")
    monkeypatch.setattr(install, "install_global", lambda args: None)
    monkeypatch.setattr(install, "_update_bashrc_block", lambda: None)
    home = render.render_claude("rb-role")
    _write(world["bundle"] / ".claude" / "skills" / "new-skill" / "SKILL.md", "---\nname: new-skill\n---\n")
    (render.rendered_root() / "gone" / "claude").mkdir(parents=True)

    install.cmd_init_unified(Namespace(profile="rb-role"))

    assert (home / "skills" / "new-skill").is_symlink()
    lines = capsys.readouterr().out.splitlines()
    assert f"{install._DIM}[--] Profile home gone kept as it was: Profile 'gone' not found{install._RESET}" in lines
    assert f"{install._GREEN}[OK]{install._RESET} Re-rendered the rb-role profile home" in lines


def test_stamp_names_bundle_commit_and_chain(world):
    from scripts.profiles import render

    head = _git(world["bundle"], "rev-parse", "HEAD").strip()
    chain = ["rb-base", "rb-kit", "rb-role"]
    assert render.stamp("rb-role") == {"bundle_commit": head, "chain": chain, "plugins": {}, "corrections": ""}
    assert render._stamp(None, []) == {"bundle_commit": "", "chain": [], "plugins": {}, "corrections": ""}
    assert render._roots(None, [("rb-role", world["role"])]) == [world["role"]]


def test_render_refuses_other_targets(world):
    from scripts.profiles import render

    with pytest.raises(ValueError, match="^copilot per-run profiles are not supported$"):
        render.render("copilot", "rb-role")


def test_cli_renders_and_refuses(world, capsys):
    from scripts.profiles import render

    out = Path.home() / ".agentihooks" / "profiles" / "rb-role" / "claude"
    assert render.main(["render", "rb-role"]) == 0
    assert capsys.readouterr().out.endswith(f"\nRendered rb-role (claude) → {out}\n")
    assert render.main(["render", "rb-role", "--target", "claude"]) == 0
    assert capsys.readouterr().out == "rb-role (claude) is up to date\n"
    assert render.main(["render", "rb-role", "--force"]) == 0
    assert capsys.readouterr().out.endswith(f"\nRendered rb-role (claude) → {out}\n")
    assert render.main(["render", "rb-role", "--target", "copilot"]) == 2
    assert capsys.readouterr().err == "copilot per-run profiles are not supported\n"
    assert render.main(["render", "rb-missing", "--target", "codex"]) == 1
    assert capsys.readouterr().err == "ERROR: Profile 'rb-missing' not found\n"


def test_cli_usage(world, capsys):
    from scripts.profiles import render

    with pytest.raises(SystemExit):
        render.main(["render", "rb-role", "--target", "bogus"])
    assert re.search(
        r"invalid choice: 'bogus' \(choose from '?claude'?, '?codex'?, '?copilot'?\)", capsys.readouterr().err
    )
    with pytest.raises(SystemExit):
        render.main([])
    assert capsys.readouterr().err.startswith("usage: agentihooks profile [-h] {render,measure}")
    with pytest.raises(SystemExit):
        render.main(["--help"])
    assert re.search(
        r"(?<!\S)render Render a profile into its own home for one harness(?!\S)", _flat(capsys.readouterr().out)
    )


def test_agentihooks_profile_dispatches_to_render(monkeypatch, capsys):
    from scripts import install

    monkeypatch.setattr("sys.argv", ["agentihooks", "profile", "render", "rb-role", "--target", "copilot"])
    with pytest.raises(SystemExit) as exc:
        install.main()
    assert exc.value.code == 2
    assert capsys.readouterr().err == "copilot per-run profiles are not supported\n"


def test_agentihooks_help_lists_profile(monkeypatch, capsys):
    from scripts import install

    monkeypatch.setattr("sys.argv", ["agentihooks", "--help"])
    with pytest.raises(SystemExit):
        install.main()
    line = r"(?<!\S)profile Render a profile into its own home: render NAME --target claude\|codex \[--force\](?!\S)"
    assert re.search(line, _flat(capsys.readouterr().out))


@pytest.fixture
def package_role(world, tmp_path, monkeypatch):
    from hooks.context import profile_chain

    roles = tmp_path / "package-roles"
    monkeypatch.setattr(profile_chain, "PACKAGE_ROLES", roles)
    role = roles / "rb-pkg"
    _write(role / "profile.yml", "name: rb-pkg\n")
    _write(role / "CLAUDE.md", "PACKAGE PERSONA MARKER\n")
    _write(
        role / ".claude" / "settings.overrides.json", json.dumps({"env": {"PACKAGE_FLAG": "1", "WINNER": "package"}})
    )
    _write(role / ".claude" / ".mcp.json", json.dumps({"mcpServers": {"pkg-srv": {"command": "pkg-server"}}}))
    _write(role / ".claude" / "rules" / "pkg-rule.md", "PACKAGE RULE MARKER\n")
    return role


def test_package_role_renders_with_no_bundle_linked(world, package_role):
    from scripts.profiles import render

    world["install"]._save_state({})
    out = render.render_claude("rb-pkg")

    assert "PACKAGE PERSONA MARKER" in (out / "CLAUDE.md").read_text()
    assert json.loads((out / "settings.json").read_text())["env"]["PACKAGE_FLAG"] == "1"
    assert json.loads((out / ".claude.json").read_text())["mcpServers"]["pkg-srv"] == {"command": "pkg-server"}
    assert "PACKAGE RULE MARKER" in (out / "CLAUDE.md").read_text()
    assert json.loads((out / render.STAMP).read_text())["chain"] == ["rb-pkg"]


def test_bundle_overlay_extending_package_role_sits_on_top(world, package_role):
    from scripts.profiles import render

    overlay = world["bundle"] / "profiles" / "rb-pkg"
    _write(overlay / "profile.yml", "name: rb-pkg\nextends: [package:rb-pkg]\n")
    _write(overlay / "CLAUDE.md", "OVERLAY PERSONA MARKER\n")
    _write(overlay / ".claude" / "settings.overrides.json", json.dumps({"env": {"WINNER": "bundle"}}))
    out = render.render_claude("rb-pkg")

    persona = (out / "CLAUDE.md").read_text()
    assert persona.index("PACKAGE PERSONA MARKER") < persona.index("OVERLAY PERSONA MARKER")
    env = json.loads((out / "settings.json").read_text())["env"]
    assert (env["PACKAGE_FLAG"], env["WINNER"]) == ("1", "bundle")
    assert json.loads((out / render.STAMP).read_text())["chain"] == ["package:rb-pkg", "rb-pkg"]
