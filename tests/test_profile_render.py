import hashlib
import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace

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


def test_a_profile_whose_own_layers_enable_plugins_is_claude_only(world):
    from scripts.profiles import plugins

    profiles = world["bundle"] / "profiles"
    _write(world["bundle"] / ".claude" / "settings.overrides.json", json.dumps({"enabledPlugins": {"g@m": True}}))
    _write(profiles / "engineer" / "profile.yml", "name: engineer\n")
    _write(profiles / "rb-front" / "profile.yml", "name: rb-front\nextends: [engineer]\n")
    front = {"enabledPlugins": {"frontend-design@claude-plugins-official": True}}
    _write(profiles / "rb-front" / ".claude" / "settings.overrides.json", json.dumps(front))
    _write(profiles / "rb-kid" / "profile.yml", "name: rb-kid\nextends: [rb-front]\n")
    _write(profiles / "rb-off" / "profile.yml", "name: rb-off\n")
    off = {"enabledPlugins": {"frontend-design@claude-plugins-official": False}}
    _write(profiles / "rb-off" / ".claude" / "settings.overrides.json", json.dumps(off))

    assert plugins.claude_only("rb-front") is True
    assert plugins.claude_only("rb-kid") is True
    assert plugins.claude_only("engineer") is False
    assert plugins.claude_only("rb-off") is False
    assert plugins.claude_only("rb-role") is False


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


CODEX_STATE = ("auth.json", "sessions", "history.jsonl", "session_index.jsonl", "hooks.json")


def _operator_codex(home: Path) -> None:
    hooks = home / ".codex" / "hooks.json"
    text = (home / ".codex" / "config.toml").read_text()
    text = 'model = "gpt-op"\nservice_tier = "fast"\nnotify = ["py", "-m", "shim"]\n' + text
    text += f'\n[hooks.state."{hooks}:pre_tool_use:0:0"]\ntrusted_hash = "sha256:aa"\n'
    text += '\n[hooks.state."/elsewhere/config.toml:stop:0:0"]\ntrusted_hash = "sha256:bb"\n'
    text += '\n[projects."/w"]\ntrust_level = "trusted"\n'
    _write(home / ".codex" / "config.toml", text)


def test_codex_render_links_into_the_claude_profile(world):
    from scripts.profiles import render

    out = render.render_codex("rb-role")

    claude = render.rendered_root() / "rb-role" / "claude"
    assert out == render.rendered_root() / "rb-role" / "codex"
    assert os.readlink(out / "AGENTS.md") == str(claude / "CLAUDE.md")
    linked = {p.name: os.readlink(p) for p in (out / "skills").iterdir() if p.is_symlink()}
    assert linked == {p.name: str(p) for p in (claude / "skills").iterdir()}
    assert {"bundle-skill", "role-skill"} <= set(linked)
    assert sorted(p.name for p in out.iterdir() if not p.is_symlink()) == ["config.toml", "skills"]
    sources = render.sources.path("rb-role", "codex", render.rendered_root())
    assert os.readlink(sources) == str(render.sources.path("rb-role", "claude", render.rendered_root()))


def test_codex_render_offers_each_command_as_a_hardlinked_skill(world):
    from scripts.profiles import render

    bundle_cmd = _write(world["bundle"] / ".claude" / "commands" / "deploy.md", "---\ndescription: Deploy\n---\nGo.\n")
    kit = world["bundle"] / "profiles" / "rb-kit" / ".claude" / "commands"
    role_cmd = _write(kit / "triage.md", "---\ndescription: Triage\nargument-hint: [n]\n---\nTriage $ARGUMENTS.\n")
    _write(kit / "bare.md", "No frontmatter, so Codex refuses it.\n")

    out = render.render_codex("rb-role")

    skills = out / "skills"
    for name, source in (("deploy", bundle_cmd), ("triage", role_cmd)):
        skill = skills / name / "SKILL.md"
        assert not skill.is_symlink()
        assert skill.samefile(source)
    assert not (skills / "bare").exists()
    assert not (out / "prompts").exists()


def test_codex_render_lets_a_skill_keep_its_name_over_a_command(world):
    from scripts.profiles import render

    _write(world["bundle"] / ".claude" / "commands" / "role-skill.md", "---\ndescription: Clash\n---\nBody.\n")

    out = render.render_codex("rb-role")

    assert (out / "skills" / "role-skill").is_symlink()


def test_codex_render_drops_the_skill_of_a_removed_command(world):
    from scripts.profiles import render

    command = _write(world["bundle"] / ".claude" / "commands" / "deploy.md", "---\ndescription: Deploy\n---\nGo.\n")
    out = render.render_codex("rb-role")
    _write(out / "skills" / ".system" / "codex" / "SKILL.md", "codex's own\n")
    command.unlink()

    render.render_codex("rb-role", force=True)

    assert not (out / "skills" / "deploy").exists()
    assert (out / "skills" / ".system" / "codex" / "SKILL.md").read_text() == "codex's own\n"


def test_codex_render_config_has_no_persona_and_only_profile_servers(world):
    from scripts.profiles import render

    home = world["home"]
    doc = tomllib.loads((render.render_codex("rb-role") / "config.toml").read_text())

    assert "developer_instructions" not in doc
    assert doc["sandbox_mode"] == "workspace-write"
    assert doc["approval_policy"] == "never"
    assert doc["features"]["hooks"] is True
    assert doc["mcp_servers"] == {"role-srv": {"command": "r"}, "bundle-srv": {"command": "b"}}
    skills = home / ".agents" / "skills"
    assert doc["skills"]["config"] == [
        {"path": str(skills / name / "SKILL.md"), "enabled": False}
        for name in ("bundle-skill", "other-skill", "role-skill")
    ]
    assert doc["project_doc_max_bytes"] == 65536
    assert doc["agentihooks"]["render"] == render.stamp("rb-role")
    operator = (home / ".codex" / "config.toml").read_bytes()
    assert doc["agentihooks"]["operator"] == hashlib.sha256(operator).hexdigest()
    assert render.render_codex("rb-role") is None


def test_codex_render_keeps_hooks_on_and_layer_servers_out(world):
    from scripts.profiles import render

    layer = "[features]\nhooks = false\n\n[mcp_servers.layer-srv]\ncommand = 'x'\n"
    _write(world["role"] / ".codex" / "config.overrides.toml", layer)

    doc = tomllib.loads((render.render_codex("rb-role") / "config.toml").read_text())

    assert doc["features"]["hooks"] is True
    assert set(doc["mcp_servers"]) == {"role-srv", "bundle-srv"}


def test_codex_render_sizes_the_doc_cap_to_a_large_persona(world):
    from scripts.profiles import render

    _write(world["role"] / ".claude" / "rules" / "big.md", "x" * 100_000 + "\n")

    out = render.render_codex("rb-role")

    persona = len((render.rendered_root() / "rb-role" / "claude" / "CLAUDE.md").read_bytes())
    assert tomllib.loads((out / "config.toml").read_text())["project_doc_max_bytes"] == int(persona * 1.25)


def test_codex_render_force_rerenders_the_claude_profile(world):
    from scripts.profiles import render

    out = render.render_codex("rb-role")
    persona = render.rendered_root() / "rb-role" / "claude" / "CLAUDE.md"
    persona.write_text("stale\n")

    assert render.render_codex("rb-role", force=True) == out
    assert "ROLE PERSONA MARKER" in (out / "AGENTS.md").read_text()


def test_codex_render_links_state_back_to_the_operator_home(world):
    from scripts.profiles import render

    home = world["home"]
    _operator_codex(home)

    out = render.render_codex("rb-role")

    for item in CODEX_STATE:
        assert os.readlink(out / item) == str(home / ".codex" / item)
    doc = tomllib.loads((out / "config.toml").read_text())
    assert doc["sqlite_home"] == str(home / ".codex")
    assert doc["hooks"]["state"] == {f"{out / 'hooks.json'}:pre_tool_use:0:0": {"trusted_hash": "sha256:aa"}}
    assert doc["projects"] == {"/w": {"trust_level": "trusted"}}
    assert (doc["model"], doc["service_tier"], doc["notify"]) == ("gpt-op", "fast", ["py", "-m", "shim"])


def test_codex_render_follows_operator_changes(world):
    from scripts.profiles import render

    out = render.render_codex("rb-role")
    _operator_codex(world["home"])

    assert render.render_codex("rb-role") == out
    assert tomllib.loads((out / "config.toml").read_text())["model"] == "gpt-op"


def test_codex_render_from_inside_a_profile_codex_home(world, monkeypatch):
    from scripts.profiles import render

    home = world["home"]
    monkeypatch.setenv("CODEX_HOME", str(render.rendered_root() / "rb-other" / "codex"))

    out = render.render_codex("rb-role")

    assert os.readlink(out / "auth.json") == str(home / ".codex" / "auth.json")
    assert "role-srv" in tomllib.loads((out / "config.toml").read_text())["mcp_servers"]


@pytest.mark.parametrize("config", [None, 'model = "gpt"\n'])
def test_codex_render_without_global_servers_or_skills(world, config):
    from scripts.profiles import render

    home = world["home"]
    (home / ".codex" / "config.toml").unlink()
    if config:
        _write(home / ".codex" / "config.toml", config)
    for skill in (home / ".agents" / "skills").iterdir():
        skill.unlink() if skill.is_file() else skill.rmdir()
    _write(world["role"] / ".codex" / "config.overrides.toml", "[mcp_servers.layer-srv]\ncommand = 'x'\n")

    doc = tomllib.loads((render.render_codex("rb-role") / "config.toml").read_text())

    assert "mcp_servers" not in doc
    assert "skills" not in doc
    assert "hooks" not in doc


def test_codex_render_retires_the_old_profile_config(world):
    from scripts.profiles import render

    codex = world["home"] / ".codex"
    old = _write(codex / "rb-role.config.toml", '[agentihooks.render]\nchain = ["rb-role"]\n')
    hand = _write(codex / "rb-other.config.toml", 'model = "hand-written"\n')

    render.render_codex("rb-role")
    render.render_codex("rb-other")

    assert not old.exists()
    assert hand.read_text() == 'model = "hand-written"\n'


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_render_leaves_global_install_untouched(world, target):
    from scripts.profiles import render

    home = world["home"]
    skip = home / ".agentihooks" / "profiles"
    before = _tree_hashes(home, skip)

    render.render(target, "rb-role")

    assert _tree_hashes(home, skip) == before


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
    _write(world["home"] / ".codex" / "rb-old.config.toml", '[agentihooks.render]\nchain = ["rb-old"]\n')

    assert render.rendered_profiles("claude") == ["rb-other", "rb-role"]
    assert render.rendered_profiles("codex") == ["rb-old", "rb-other"]
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
    line = r"(?<!\S)profile Render a profile into its own home: render NAME --target claude\|codex \[--force\] \[--out DIR \[--bundle DIR\]\](?!\S)"
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


def _scratch_bundle(world, tmp_path: Path) -> Path:
    bundle = tmp_path / "scratch-bundle"
    shutil.copytree(world["bundle"], bundle)
    _write(bundle / "profiles" / "rb-role" / "CLAUDE.md", "SCRATCH PERSONA MARKER\n")
    return bundle


def test_scratch_render_writes_only_under_its_home(world, tmp_path, capfd):
    from scripts.profiles import render

    live = render.render_claude("rb-role")
    before = _tree_hashes(render.rendered_root(), tmp_path / "none")
    links = sorted((p, p.readlink()) for p in render.rendered_root().rglob("*") if p.is_symlink())
    bundle, out = _scratch_bundle(world, tmp_path), tmp_path / "scratch-home"
    capfd.readouterr()

    assert render.main(["render", "rb-role", "--out", str(out), "--bundle", str(bundle)]) == 0

    home = out / "profiles" / "rb-role" / "claude"
    assert capfd.readouterr().out.endswith(f"Rendered rb-role (claude) → {home}\n")
    assert "SCRATCH PERSONA MARKER" in (home / "CLAUDE.md").read_text()
    assert "SCRATCH PERSONA MARKER" not in (live / "CLAUDE.md").read_text()
    assert _tree_hashes(render.rendered_root(), tmp_path / "none") == before
    assert sorted((p, p.readlink()) for p in render.rendered_root().rglob("*") if p.is_symlink()) == links
    assert {item: (home / item).readlink() for item in SHARED} == {
        item: Path.home() / ".claude" / item for item in SHARED
    }


def test_scratch_render_reads_corrections_from_its_own_home(world, tmp_path, monkeypatch):
    from hooks.context import injection_trace
    from scripts.profiles import render, sources

    monkeypatch.delenv("AGENTIHOOKS_GATE_QUARANTINE", raising=False)
    bundle, out = _scratch_bundle(world, tmp_path), tmp_path / "scratch-home"
    rule = sources.source(bundle / ".claude" / "rules" / "bundle-rule.md")
    with monkeypatch.context() as scratch:
        scratch.setattr("hooks.config.AGENTIHOOKS_HOME", out)
        injection_trace.record("proof-1", "rule", rule, "BUNDLE RULE MARKER", {})
        injection_trace.correct("proof-1", rule, "/repos/proof", "a planted proof correction")

    assert render.main(["render", "rb-role", "--out", str(out), "--bundle", str(bundle)]) == 0

    notice = "> CORRECTION: this file is marked wrong for the proof repo: a planted proof correction."
    assert notice in (out / "profiles" / "rb-role" / "claude" / "CLAUDE.md").read_text()
    assert "CORRECTION" not in (render.render_claude("rb-role", force=True) / "CLAUDE.md").read_text()


def test_scratch_render_defaults_to_the_linked_bundle_and_passes_force(world, tmp_path, capfd):
    from scripts.profiles import render

    out = tmp_path / "scratch-home"
    home = out / "profiles" / "rb-role" / "claude"

    assert render.main(["render", "rb-role", "--out", str(out)]) == 0
    assert "ROLE PERSONA MARKER" in (home / "CLAUDE.md").read_text()
    assert capfd.readouterr().out.endswith(f"Rendered rb-role (claude) → {home}\n")
    assert render.main(["render", "rb-role", "--out", str(out)]) == 0
    assert capfd.readouterr().out.endswith("rb-role (claude) is up to date\n")
    assert render.main(["render", "rb-role", "--out", str(out), "--force"]) == 0
    assert capfd.readouterr().out.endswith(f"Rendered rb-role (claude) → {home}\n")
    assert not (render.rendered_root() / "rb-role").exists()


def test_scratch_render_refuses_a_bundle_without_a_home(world, tmp_path, capsys):
    from scripts.profiles import render

    with pytest.raises(SystemExit):
        render.main(["render", "rb-role", "--bundle", str(world["bundle"])])
    assert "--bundle needs --out" in capsys.readouterr().err
    assert not render.rendered_root().exists()


def test_scratch_render_runs_this_checkout_in_a_child(world, tmp_path, monkeypatch):
    from scripts.profiles import render

    calls = []
    monkeypatch.setattr(
        render.subprocess, "run", lambda argv, **kw: calls.append((argv, kw)) or subprocess.CompletedProcess(argv, 3)
    )
    monkeypatch.chdir(tmp_path)

    assert render.main(["render", "rb-role", "--target", "codex", "--out", "rel-home"]) == 3

    ((argv, kw),) = calls
    assert argv[1:] == ["-m", "scripts.profiles.render", "render", "rb-role", "--target", "codex"]
    assert Path(kw["cwd"]) / "scripts" / "profiles" / "render.py" == Path(render.__file__).resolve()
    assert kw["env"]["AGENTIHOOKS_HOME"] == str(tmp_path / "rel-home")
    assert kw["env"]["AGENTIHOOKS_BUNDLE_PATH"] == str(world["bundle"])


def test_scratch_render_options_are_documented(capsys):
    from scripts.profiles import render

    with pytest.raises(SystemExit):
        render.main(["render", "--help"])
    out = _flat(capsys.readouterr().out)
    assert "--out OUT Render into this scratch agentihooks home, not the live one" in out
    assert "--bundle BUNDLE Bundle for --out (default: the linked bundle)" in out
    with pytest.raises(SystemExit):
        render.main(["render", "rb-role", "--bundle", "b"])
    assert capsys.readouterr().err.endswith("render: error: --bundle needs --out\n")


@pytest.fixture
def worktree_run(world, tmp_path, monkeypatch):
    install = world["install"]
    installed = tmp_path / "installed-agentihooks"
    monkeypatch.setattr(install, "AGENTIHOOKS_ROOT", tmp_path / "worktrees" / "agentihooks" / "eng-1")
    monkeypatch.setattr(install, "install_root", lambda: installed)
    return installed


def test_render_from_a_worktree_leaves_the_live_home_untouched(world, worktree_run, monkeypatch, capsys):
    from scripts.profiles import render

    monkeypatch.setattr(render.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(Path.home())))
    settings = _write(render.rendered_root() / "rb-role" / "claude" / "settings.json", '{"live": true}\n')
    before = _tree_hashes(render.rendered_root(), Path("/none"))

    for target in ("claude", "codex"):
        with pytest.raises(ValueError, match="scratch home"):
            render.render(target, "rb-role", force=True)
    assert render.main(["render", "rb-role", "--force"]) == 1

    assert "agentihooks profile render rb-role --out" in capsys.readouterr().err
    assert settings.read_bytes() == b'{"live": true}\n'
    assert _tree_hashes(render.rendered_root(), Path("/none")) == before


def test_render_takes_the_hook_root_from_the_installed_agentihooks(world, worktree_run):
    from scripts.profiles import render

    text = (render.render_claude("rb-role") / "settings.json").read_text()

    assert f"cd {worktree_run} && " in text
    assert str(world["install"].AGENTIHOOKS_ROOT) not in text


def test_install_root_is_the_editable_source(world, tmp_path, monkeypatch):
    from importlib.metadata import PackageNotFoundError

    install = world["install"]
    source = tmp_path / "agentihooks src"
    records = [
        ({"url": source.as_uri(), "dir_info": {"editable": True}}, source),
        ({"url": "https://x", "archive_info": {}}, install.AGENTIHOOKS_ROOT),
        ({"url": source.as_uri(), "dir_info": {}}, install.AGENTIHOOKS_ROOT),
        ("{not json", install.AGENTIHOOKS_ROOT),
        (None, install.AGENTIHOOKS_ROOT),
    ]

    class Dist:
        def __init__(self, record):
            self.record = record

        def read_text(self, name):
            if name != "direct_url.json" or self.record is None:
                return None
            return self.record if isinstance(self.record, str) else json.dumps(self.record)

    def distribution(record):
        def find(name):
            if name != "agentihooks":
                raise PackageNotFoundError(name)
            return Dist(record)

        return find

    for record, root in records:
        monkeypatch.setattr(install.metadata, "distribution", distribution(record))
        assert install.install_root() == root

    def missing(name):
        raise PackageNotFoundError(name)

    monkeypatch.setattr(install.metadata, "distribution", missing)
    assert install.install_root() == install.AGENTIHOOKS_ROOT
