import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
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
def world(tmp_path, monkeypatch, _isolate_real_user_paths):
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

    assert out == (home / ".agentihooks" / "profiles" / "rb-role" / "claude").resolve()
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
    assert persona.count(render.FOOTER) == 1
    assert "\n\nNone\n" not in persona
    assert render.binding.inspect(out, "rb-role", "claude")["profile"] == "rb-role"


@pytest.mark.parametrize("role", ["engineer", "cicd", "planner", "master", "qa", "frontend"])
@pytest.mark.parametrize("target", ["claude", "codex"])
def test_swarm_roles_render_only_the_task_browser(world, role, target):
    from scripts.profiles import render

    root = world["bundle"] / "profiles" / role
    extends = "package:engineer" if role == "frontend" else f"package:{role}"
    _write(root / "profile.yml", f"name: {role}\nextends: [{extends}]\n")
    _write(
        root / ".claude" / ".mcp.json",
        json.dumps(
            {
                "mcpServers": {
                    "playwright-cmd": {"command": "cmd.exe", "args": ["/c", "npx", "@playwright/mcp@latest"]},
                    "playwright-attached": {"url": "http://localhost:9222/mcp"},
                }
            }
        ),
    )
    out = render.render(target, role)
    if target == "claude":
        servers = json.loads((out / ".claude.json").read_text())["mcpServers"]
    else:
        servers = tomllib.loads((out / "config.toml").read_text())["mcp_servers"]
    browsers = {k: v for k, v in servers.items() if "playwright" in k}
    assert list(browsers) == ["playwright-cmd"]
    assert browsers["playwright-cmd"]["command"] == str(world["python"])
    assert browsers["playwright-cmd"]["args"] == ["-I", "-m", "scripts.profiles.browser"]
    assert render.stamp(role)["browser"] == browsers["playwright-cmd"]
    settings = json.loads((render.rendered_root() / role / "claude" / "settings.json").read_text())
    assert not settings["enabledPlugins"].get("playwright@claude-plugins-official", False)


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_a_role_overlay_keeps_the_logged_in_extension_browser(world, target):
    from scripts.profiles import render

    extension = {
        "command": "cmd.exe",
        "args": ["/c", "npx", "@playwright/mcp@latest", "--extension"],
        "env": {"PLAYWRIGHT_MCP_EXTENSION_TOKEN": "${PLAYWRIGHT_EXT_TOKEN_NESTORCOLT_GMAIL}"},
    }
    for role in ("master", "engineer"):
        _write(world["bundle"] / "profiles" / role / "profile.yml", f"name: {role}\nextends: [package:{role}]\n")
    _write(
        world["bundle"] / "profiles" / "master" / ".claude" / ".mcp.json",
        json.dumps({"mcpServers": {"playwright-ext-nestorcolt-gmail": extension, "playwright-tcc": extension}}),
    )

    def browsers(role):
        out = render.render(target, role)
        if target == "claude":
            servers = json.loads((out / ".claude.json").read_text())["mcpServers"]
        else:
            servers = tomllib.loads((out / "config.toml").read_text())["mcp_servers"]
        return {k: v for k, v in servers.items() if "playwright" in k}

    master = browsers("master")
    assert sorted(master) == ["playwright-cmd", "playwright-ext-nestorcolt-gmail"]
    assert "--extension" in master["playwright-ext-nestorcolt-gmail"]["args"]
    assert list(browsers("engineer")) == ["playwright-cmd"]


def test_operator_profile_keeps_its_browser(world):
    from scripts.profiles import render

    root = world["bundle"] / "profiles" / "operator"
    _write(root / "profile.yml", "name: operator\n")
    original = {"command": "cmd.exe", "args": ["/c", "npx", "@playwright/mcp@latest"]}
    _write(root / ".claude" / ".mcp.json", json.dumps({"mcpServers": {"playwright-cmd": original}}))
    out = render.render_claude("operator")
    assert json.loads((out / ".claude.json").read_text())["mcpServers"]["playwright-cmd"] == original


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

    fresh = render.render_claude("rb-role")
    assert fresh not in (None, out)
    assert not (fresh / "rules").exists()
    assert (world["bundle"] / ".claude" / "rules" / "bundle-rule.md").read_text() == "BUNDLE RULE MARKER\n"
    assert render.render_claude("rb-role") is None


def test_claude_render_refreshes_folded_rules(world):
    from scripts.profiles import render

    out = render.render_claude("rb-role")
    source = world["bundle"] / ".claude" / "rules" / "bundle-rule.md"
    _write(source, "---\npaths: ['**/*.py']\n---\nUPDATED BUNDLE RULE\n")

    fresh = render.render_claude("rb-role", force=True)
    assert fresh != out
    persona = (fresh / "CLAUDE.md").read_text()
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
    before = _home_files(out)

    payload = refresh_rules(out / "rules", out / "CLAUDE.md", out / "CLAUDE.local.md", False)

    fresh = render.profile_dir("rb-role") / "claude"
    assert fresh != out and _home_files(out) == before
    assert "UPDATED RENDERED RULE" in (fresh / "CLAUDE.md").read_text()
    assert not (fresh / "rules").exists()
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


def test_an_operator_home_carries_the_operator_user_scope_plugins(world):
    from scripts.profiles import render

    home, bundle = world["home"], world["bundle"]
    operator = {"model": "opus", "enabledPlugins": {"mine@m": True, "kit@m": False, "muted@m": True, "off@m": False}}
    _write(home / ".claude" / "settings.json", json.dumps(operator))
    plugin = {"kind": "claude-plugin", "check": ["true"], "install": ["true"]}
    _write(bundle / "deps.json", json.dumps({"deps": [{**plugin, "id": "fleet@m"}]}))
    kit = {"enabledPlugins": {"kit@m": True, "muted@m": False}}
    _write(bundle / "profiles" / "rb-kit" / ".claude" / "settings.overrides.json", json.dumps(kit))

    out = render.render_claude("rb-role")

    assert json.loads((out / "settings.json").read_text())["enabledPlugins"] == {"mine@m": True, "kit@m": True}
    assert json.loads((home / ".claude" / "settings.json").read_text()) == operator


MATTPOCOCK, PLAYWRIGHT = "mattpocock-skills@claude-plugins-official", "playwright@claude-plugins-official"


@pytest.mark.parametrize(
    ("role", "plugins"),
    [("engineer", [MATTPOCOCK]), ("cicd", [MATTPOCOCK]), ("planner", [MATTPOCOCK]), ("master", [])],
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


def _install_in_home(out: Path, plugin: str) -> None:
    settings = json.loads((out / "settings.json").read_text())
    settings["enabledPlugins"] = {**settings["enabledPlugins"], plugin: True}
    (out / "settings.json").write_text(json.dumps(settings))


@pytest.mark.parametrize(("name", "rendered"), [("rb-role", {"mine@m": True}), ("engineer", {MATTPOCOCK: True})])
def test_a_plugin_installed_inside_a_home_survives_the_next_render(world, name, rendered):
    from scripts.profiles import render

    _write(world["bundle"] / "profiles" / "engineer" / "profile.yml", "name: engineer\nextends: [rb-base]\n")
    _write(world["home"] / ".claude" / "settings.json", json.dumps({"enabledPlugins": {"mine@m": True}}))
    _install_in_home(render.render_claude(name), "local@m")

    out = render.render_claude(name, force=True)

    assert json.loads((out / "settings.json").read_text())["enabledPlugins"] == {**rendered, "local@m": True}


def test_init_carries_a_user_scope_install_into_the_rendered_home(world):
    from scripts.profiles import render

    render.render_claude("rb-role")
    _write(world["home"] / ".claude" / "settings.json", json.dumps({"enabledPlugins": {"new@m": True}}))

    world["install"]._rerender_profile_homes("claude")

    out = render.profile_dir("rb-role") / "claude"
    assert json.loads((out / "settings.json").read_text())["enabledPlugins"] == {"new@m": True}


def test_a_home_without_a_plugin_record_keeps_what_is_enabled_inside_it(world):
    from scripts.profiles import render

    out = render.render_claude("rb-role")
    _install_in_home(out, "local@m")
    (out / render.PLUGINS).unlink()

    out = render.render_claude("rb-role", force=True)

    assert json.loads((out / "settings.json").read_text())["enabledPlugins"] == {"local@m": True}


def test_a_home_without_a_stamp_still_drops_a_plugin_its_source_dropped(world):
    from scripts.profiles import render

    kit = world["bundle"] / "profiles" / "rb-kit" / ".claude" / "settings.overrides.json"
    _write(kit, json.dumps({"enabledPlugins": {"kit@m": True}}))
    out = render.render_claude("rb-role")
    _install_in_home(out, "local@m")
    (out / render.STAMP).unlink()
    _write(kit, json.dumps({}))

    out = render.render_claude("rb-role")

    assert json.loads((out / "settings.json").read_text())["enabledPlugins"] == {"local@m": True}


def test_a_home_built_on_a_package_role_leaves_out_the_operator_plugins(world):
    from scripts.profiles import render

    _write(world["bundle"] / "profiles" / "rb-front" / "profile.yml", "name: rb-front\nextends: [package:engineer]\n")
    _write(world["home"] / ".claude" / "settings.json", json.dumps({"enabledPlugins": {"mine@m": True}}))

    out = render.render_claude("rb-front")

    assert json.loads((out / "settings.json").read_text())["enabledPlugins"] == {MATTPOCOCK: True}


def test_a_plugin_the_bundle_layer_drops_leaves_the_home(world):
    from scripts.profiles import render

    layer = world["bundle"] / ".claude" / "settings.overrides.json"
    _write(layer, json.dumps({"enabledPlugins": {"bundle@m": True}}))
    render.render_claude("rb-role")
    _write(layer, json.dumps({}))

    out = render.render_claude("rb-role", force=True)

    assert json.loads((out / "settings.json").read_text())["enabledPlugins"] == {}


def test_a_render_inside_a_profile_home_reads_the_operator_plugins(world, monkeypatch):
    from scripts.profiles import render

    inside = _write(world["home"] / "inside" / "settings.json", json.dumps({"enabledPlugins": {"inner@m": True}}))
    _write(world["home"] / ".claude" / "settings.json", json.dumps({"enabledPlugins": {"mine@m": True}}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(inside.parent))

    out = render.render_claude("rb-role")

    assert json.loads((out / "settings.json").read_text())["enabledPlugins"] == {"mine@m": True}


def test_a_profile_disable_beats_a_plugin_installed_inside_the_home(world):
    from scripts.profiles import render

    _install_in_home(render.render_claude("rb-role"), "local@m")
    off = {"enabledPlugins": {"local@m": False}}
    _write(world["bundle"] / "profiles" / "rb-kit" / ".claude" / "settings.overrides.json", json.dumps(off))

    out = render.render_claude("rb-role")

    assert json.loads((out / "settings.json").read_text())["enabledPlugins"] == {}


def test_a_plugin_the_render_wrote_leaves_when_its_source_drops_it(world):
    from scripts.profiles import render

    kit = world["bundle"] / "profiles" / "rb-kit" / ".claude" / "settings.overrides.json"
    settings = world["home"] / ".claude" / "settings.json"
    _write(settings, json.dumps({"enabledPlugins": {"mine@m": True}}))
    _write(kit, json.dumps({"enabledPlugins": {"kit@m": True}}))
    render.render_claude("rb-role")

    _write(settings, json.dumps({"enabledPlugins": {}}))
    _write(kit, json.dumps({}))
    out = render.render_claude("rb-role")

    assert json.loads((out / "settings.json").read_text())["enabledPlugins"] == {}


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

    assert json.loads((out / "settings.json").read_text())["enabledPlugins"] == {}


def test_claude_render_keeps_a_home_without_brain_off_the_brain_channel(world):
    from scripts.profiles import render

    out = render.render_claude("rb-role")

    assert "AGENTIHOOKS_BASE_CHANNELS" not in json.loads((out / "settings.json").read_text())["env"]
    assert render.channels("rb-role") == ""
    assert json.loads((out / render.STAMP).read_text())["overlays"] == []


@pytest.fixture
def named_brain(world, tmp_path):
    brain = tmp_path / "kernel" / "profiles" / "brain"
    _write(brain / "profile.yml", "name: brain\n")
    install = world["install"]
    state = install._load_state()
    state["linked_profiles"] = [{"name": "brain", "path": str(brain)}]
    install._save_state(state)
    profile = world["role"] / "profile.yml"
    profile.write_text(profile.read_text() + "allowedOverlays: [brain]\n")
    return brain


def test_brain_joins_the_profile_channels_once(world, named_brain):
    from scripts.profiles import render

    overrides = world["role"] / ".claude" / "settings.overrides.json"
    settings = json.loads(overrides.read_text())
    settings["env"] = {"AGENTIHOOKS_BASE_CHANNELS": "amygdala, ops"}
    overrides.write_text(json.dumps(settings))
    assert render.channels("rb-role") == "amygdala,ops,brain"
    out = render.render_claude("rb-role")
    assert json.loads((out / "settings.json").read_text())["env"]["AGENTIHOOKS_BASE_CHANNELS"] == "amygdala,ops,brain"

    settings["env"] = {"AGENTIHOOKS_BASE_CHANNELS": "brain,amygdala"}
    overrides.write_text(json.dumps(settings))
    out = render.render_claude("rb-role", force=True)
    assert json.loads((out / "settings.json").read_text())["env"]["AGENTIHOOKS_BASE_CHANNELS"] == "brain,amygdala"
    assert render.channels("rb-role") == "brain,amygdala"
    assert json.loads((out / render.STAMP).read_text())["overlays"] == ["brain"]


def test_channels_read_the_bundle_layer(world):
    from scripts.profiles import render

    overrides = {"env": {"AGENTIHOOKS_BASE_CHANNELS": "amygdala"}}
    (world["bundle"] / ".claude" / "settings.overrides.json").write_text(json.dumps(overrides))

    assert render.channels("rb-role") == "amygdala"


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
    assert set(claude_json) == (
        {"mcpServers", "hasCompletedOnboarding", "userID"}
        if operator and "u1" in operator
        else {"mcpServers", "hasCompletedOnboarding"}
    )


@pytest.mark.parametrize("role", ["frontend", "engineer"])
@pytest.mark.parametrize("completed", [None, False])
def test_fresh_role_home_completes_onboarding_without_operator_state(world, role, completed):
    from scripts.profiles import render

    _write(world["bundle"] / "profiles" / role / "profile.yml", f"name: {role}\n")
    source = world["home"] / ".claude.json"
    source.unlink()
    if completed is not None:
        source.write_text(json.dumps({"hasCompletedOnboarding": completed}))

    out = render.render_claude(role)

    assert json.loads((out / ".claude.json").read_text())["hasCompletedOnboarding"] is True
    if completed is None:
        assert not source.exists()
    else:
        assert json.loads(source.read_text()) == {"hasCompletedOnboarding": completed}


def test_cached_role_home_fills_missing_onboarding_without_resetting_state(world):
    from scripts.profiles import render

    out = render.render_claude("rb-role")
    path = out / ".claude.json"
    doc = json.loads(path.read_text())
    del doc["hasCompletedOnboarding"]
    doc["numStartups"] = 7
    doc["theme"] = "light"
    path.write_text(json.dumps(doc))
    operator = (world["home"] / ".claude.json").read_bytes()

    fresh = render.render_claude("rb-role")
    assert fresh != out

    actual = json.loads((fresh / ".claude.json").read_text())
    assert actual == {**doc, "hasCompletedOnboarding": True}
    assert json.loads(path.read_text()) == doc
    assert (world["home"] / ".claude.json").read_bytes() == operator


def test_role_rerender_preserves_explicit_onboarding_and_runtime_state(world):
    from scripts.profiles import render

    out = render.render_claude("rb-role")
    path = out / ".claude.json"
    doc = json.loads(path.read_text())
    doc.update(hasCompletedOnboarding=False, theme="light", numStartups=11)
    path.write_text(json.dumps(doc))

    fresh = render.render_claude("rb-role", force=True)
    assert fresh != out
    assert json.loads((fresh / ".claude.json").read_text()) == doc


def test_claude_render_gives_each_home_its_own_plans_folder(world):
    from scripts.profiles import render

    out = render.render_claude("rb-role")
    assert (out / "plans").is_dir() and not (out / "plans").is_symlink()

    (out / "plans").rmdir()
    (out / "plans").symlink_to(world["home"] / ".claude" / "plans")
    fresh = render.render_claude("rb-role", force=True)
    assert (fresh / "plans").is_dir() and not (fresh / "plans").is_symlink()


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
    fresh = render.render_claude("rb-role", force=True)
    assert fresh != out

    claude_json = json.loads((fresh / ".claude.json").read_text())
    assert claude_json["numStartups"] == 7
    assert "stale" not in claude_json["mcpServers"]
    assert not (fresh / "skills" / "gone").is_symlink()


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

    claude = out.parent / "claude"
    assert out == (render.rendered_root() / "rb-role" / "codex").resolve()
    assert os.readlink(out / "AGENTS.md") == str(claude / "CLAUDE.md")
    assert render.binding.inspect(out, "rb-role", "codex")["profile"] == "rb-role"
    linked = {p.name: os.readlink(p) for p in (out / "skills").iterdir() if p.is_symlink()}
    assert linked == {p.name: str(p) for p in (claude / "skills").iterdir()}
    assert {"bundle-skill", "role-skill"} <= set(linked)
    assert sorted(p.name for p in out.iterdir() if not p.is_symlink()) == sorted(
        [".profile-binding.json", render.STAMP, "config.toml", "skills"]
    )
    sources = render.sources.path(out.parent.name, "codex", out.parent.parent)
    assert os.readlink(sources) == str(render.sources.path(out.parent.name, "claude", out.parent.parent))


ROLE_TOOLSET = "http://gw.example/toolset/rb-role/mcp"
WHOLE_CATALOGUE = "http://gw.example/mcp/"


@pytest.fixture
def copilot_gateway(world, monkeypatch):
    monkeypatch.setenv("GW_KEY", "k-test")
    whole = {"mcpServers": {"gateway-tools": {"type": "http", "url": WHOLE_CATALOGUE}}}
    _write(world["home"] / ".copilot" / "mcp-config.json", json.dumps(whole))
    toolset = {"type": "http", "url": ROLE_TOOLSET, "headers": {"Authorization": "Bearer ${GW_KEY}"}}
    _declare(world, **{"gateway-tools": {**toolset, "default_tools_approval_mode": "approve"}})


def test_copilot_render_names_the_role_toolset_not_the_whole_catalogue(world, copilot_gateway):
    from scripts.profiles import render

    out = render.render_copilot("rb-role")

    assert out == (render.rendered_root() / "rb-role" / "copilot").resolve()
    text = (out / "mcp-config.json").read_text()
    assert json.loads(text)["mcpServers"]["gateway-tools"] == {
        "type": "http",
        "url": ROLE_TOOLSET,
        "headers": {"Authorization": "Bearer k-test"},
        "auth": False,
        "oidc": False,
    }
    assert WHOLE_CATALOGUE not in text
    operator = json.loads((world["home"] / ".copilot" / "mcp-config.json").read_text())
    assert operator["mcpServers"]["gateway-tools"]["url"] == WHOLE_CATALOGUE


def test_copilot_render_links_the_role_persona_and_operator_state(world, copilot_gateway):
    from scripts.profiles import render

    out = render.render_copilot("rb-role")

    assert os.readlink(out / "copilot-instructions.md") == str(out.parent / "claude" / "CLAUDE.md")
    for item in render.COPILOT_STATE:
        assert os.readlink(out / item) == str(world["home"] / ".copilot" / item)
    assert render.render_copilot("rb-role") is None
    assert render.rendered_profiles("copilot") == ["rb-role"]
    forced = render.render_copilot("rb-role", force=True)
    assert forced != out and ROLE_TOOLSET in (forced / "mcp-config.json").read_text()
    assert os.readlink(forced / "copilot-instructions.md") == str(forced.parent / "claude" / "CLAUDE.md")


def test_copilot_render_follows_a_rotated_gateway_key_into_a_private_file(world, copilot_gateway, monkeypatch):
    from scripts.profiles import render

    first = render.render_copilot("rb-role")
    monkeypatch.setenv("GW_KEY", "k-rotated")

    out = render.render_copilot("rb-role")

    assert out != first and out == render.profile_dir("rb-role") / "copilot"
    headers = json.loads((out / "mcp-config.json").read_text())["mcpServers"]["gateway-tools"]["headers"]
    assert headers == {"Authorization": "Bearer k-rotated"}
    assert (out / "mcp-config.json").stat().st_mode & 0o777 == 0o600


def test_copilot_render_keeps_a_declared_tool_allowlist(world, copilot_gateway):
    from scripts.profiles import render

    _declare(
        world, lf={"type": "http", "url": "http://lf.example/mcp", "enabled_tools": READS, "disabled_tools": ["x"]}
    )

    entry = json.loads((render.render_copilot("rb-role") / "mcp-config.json").read_text())["mcpServers"]["lf"]

    assert entry["tools"] == READS
    assert entry["excludeTools"] == ["x"]


def test_copilot_render_keeps_an_empty_tool_allowlist_closed(world, copilot_gateway):
    from scripts.profiles import render

    _declare(world, lf={"type": "http", "url": "http://lf.example/mcp", "enabled_tools": []})

    entry = json.loads((render.render_copilot("rb-role") / "mcp-config.json").read_text())["mcpServers"]["lf"]

    assert entry["tools"] == []


def test_copilot_render_writes_the_stamped_bundle_servers_as_indented_json(world, copilot_gateway):
    from scripts.profiles import render

    out = render.render_copilot("rb-role")

    text = (out / "mcp-config.json").read_text()
    assert text == json.dumps(json.loads(text), indent=2) + "\n"
    assert "bundle-srv" in json.loads(text)["mcpServers"]
    assert json.loads((out / render.STAMP).read_text()) == render.stamp("rb-role")


def test_forced_copilot_render_starts_a_new_home_beside_a_fresh_claude_one(world, copilot_gateway):
    from scripts.profiles import render

    claude = render.render_claude("rb-role")

    out = render.render_copilot("rb-role", force=True)

    assert out.parent != claude.parent
    assert out == render.profile_dir("rb-role") / "copilot"


def test_copilot_renders_an_overlay_set_into_its_own_home(world, overlays, copilot_gateway, monkeypatch):
    from scripts.profiles import render

    gateway = {"type": "http", "url": ROLE_TOOLSET, "headers": {"Authorization": "Bearer ${GW_KEY}"}}
    _write(overlays / "rb-eng" / ".claude" / ".mcp.json", json.dumps({"mcpServers": {"gw": gateway}}))
    out = render.render_copilot("rb-eng", overlays=["ov-a"])

    assert out.parent == render.profile_dir("rb-eng", ["ov-a"])
    assert json.loads((out / render.STAMP).read_text())["overlays"] == ["ov-a"]
    assert "OV-A RULE MARKER" in (out / "copilot-instructions.md").read_text()
    assert render.render_copilot("rb-eng", overlays=["ov-a"]) is None
    forced = render.render_copilot("rb-eng", force=True, overlays=["ov-a"])
    assert forced == render.profile_dir("rb-eng", ["ov-a"]) / "copilot"
    assert "OV-A RULE MARKER" in (forced / "copilot-instructions.md").read_text()
    monkeypatch.setenv("GW_KEY", "k-rotated")
    stale = render.render_copilot("rb-eng", overlays=["ov-a"])
    assert stale != forced and stale == render.profile_dir("rb-eng", ["ov-a"]) / "copilot"
    assert "OV-A RULE MARKER" in (stale / "copilot-instructions.md").read_text()


def test_init_re_renders_each_copilot_role_home(world, copilot_gateway, monkeypatch):
    from scripts.profiles import render

    first = render.render_copilot("rb-role")
    monkeypatch.setenv("GW_KEY", "k-init")

    world["install"]._rerender_profile_homes("copilot")

    out = render.rendered_root() / "rb-role" / "copilot"
    assert out.resolve() != first
    assert "Bearer k-init" in (out / "mcp-config.json").read_text()


def test_profile_render_cli_renders_a_copilot_home(world, copilot_gateway, capsys):
    from scripts.profiles import render

    assert render.main(["render", "rb-role", "--target", "copilot"]) == 0

    assert ROLE_TOOLSET in (render.rendered_root() / "rb-role" / "copilot" / "mcp-config.json").read_text()
    assert "Rendered rb-role (copilot)" in capsys.readouterr().out


def test_codex_master_replaces_monitor_instructions_without_changing_claude(world):
    from scripts.profiles import render

    _write(world["bundle"] / "profiles" / "master" / "profile.yml", "name: master\nextends: [package:master]\n")
    _write(
        world["bundle"] / ".claude" / "rules" / "waiting.md",
        "# Process Watching\n\n- Start a `Monitor` on long running work.\n"
        "- `Monitor` watches processes; `CronCreate` schedules work.\n\n"
        "# Preserve\n\nDo not mutate the master pane or expose credentials.\n",
    )
    _commit(world["bundle"], "master")

    out = render.render_codex("master")
    persona = (out / "AGENTS.md").read_text()
    claude = (render.rendered_root() / "master" / "claude" / "CLAUDE.md").read_text()
    assert "Monitor" not in persona
    assert "agentihooks msg inbox" in persona
    assert "agentihooks swarm <slug> wait --inbox" in persona
    assert "Do not mutate the master pane or expose credentials." in persona
    assert "Start a `Monitor`" in claude
    assert not (out / "AGENTS.md").is_symlink()
    assert render.render_codex("master") is None


def test_codex_master_updates_an_old_linked_persona(world):
    from scripts.profiles import render

    _write(world["bundle"] / "profiles" / "master" / "profile.yml", "name: master\nextends: [package:master]\n")
    _commit(world["bundle"], "master")
    out = render.render_codex("master")
    agents = out / "AGENTS.md"
    agents.unlink()
    agents.symlink_to(out.parent / "claude" / "CLAUDE.md")

    fresh = render.render_codex("master")
    assert fresh not in (None, out)
    assert not (fresh / "AGENTS.md").is_symlink()


def test_an_explicit_packaged_codex_master_uses_inbox_waits(world):
    from scripts.profiles import render

    out = render.render_codex("package:master")
    persona = (out / "AGENTS.md").read_text()
    assert "Monitor" not in persona
    assert "agentihooks swarm <slug> wait --inbox" in persona
    assert not (out / "AGENTS.md").is_symlink()


def test_codex_master_keeps_mixed_instruction_responsibilities():
    from scripts.profiles import codex_master

    text = (
        "- Start a `Monitor` immediately; it is read-only; keep working.\n"
        "| Working a ledger | join, handle OPERATOR lines, ack, then use a `Monitor`. |\n"
        "- Push triggers CI, deployment and rollout; start a `Monitor`.\n"
        "Monitor its checks, fix failures, and merge immediately.\n"
    )
    result = codex_master.persona(text)
    assert (
        result
        == (
            "- Start a `agentihooks swarm <slug> wait --inbox` immediately; it is read-only; keep working.\n"
            "| Working a ledger | join, handle OPERATOR lines, ack, then use a `agentihooks swarm <slug> wait --inbox`. |\n"
            "- Push triggers CI, deployment and rollout; start a `agentihooks swarm <slug> wait --inbox`.\n"
            "Watch its checks, fix failures, and merge immediately.\n"
        )
        + "\n"
        + codex_master.waiting("<slug>")
        + "\n"
    )


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("heading", ["## Next", "## Next   "])
def test_codex_master_adapts_valid_handoff_headings_without_changing_other_sections(newline, heading):
    from scripts.profiles import codex_master

    action = "Rearm a Monitor on the ledger and inspect the pending tasks."
    document = newline.join(
        ["# Handoff v2", "## Intent", "Keep promises.", heading, action, "## Read first", "None", ""]
    )
    task = {"handoff": document, "transfer": {"next": action, "id": "transfer", "handoff": "saved"}}
    adapted = codex_master.handoff(task, "sw")
    expected = "Read agentihooks msg inbox and run agentihooks swarm sw wait --inbox and inspect the pending tasks."
    assert adapted == {
        "handoff": document.replace(action, expected),
        "transfer": {"next": expected, "id": "transfer", "handoff": "saved"},
    }
    assert task["handoff"] == document


def test_codex_master_preserves_a_handoff_without_a_next_section():
    from scripts.profiles import codex_master

    task = {"handoff": "# Handoff v2\n## Intent\nKeep promises.\n"}
    assert codex_master.handoff(task, "sw") == task
    assert codex_master.handoff({}, "sw") == {}
    assert codex_master.next_action("Inspect the checks.", "sw") == "Inspect the checks."


def test_codex_master_handoff_ignores_fenced_headings_and_preserves_the_preamble():
    from scripts.profiles import codex_master

    action = "Rearm a Monitor on the ledger."
    prefix = (
        "# Handoff v2\nA previous Monitor was running.\n## Intent\n"
        "  ```text\n## Next\nRearm a Monitor on the example ledger.\n  ```\n"
    )
    task = {"handoff": prefix + "## Next\n" + action + "\n## Read first\nNone\n"}
    assert codex_master.handoff(task, "sw")["handoff"] == (
        prefix + "## Next\nRead agentihooks msg inbox and run agentihooks swarm sw wait --inbox.\n## Read first\nNone\n"
    )


def test_codex_master_waiting_instruction_names_the_foreground_return_contract():
    from scripts.profiles import codex_master

    assert codex_master.waiting("sw") == (
        "Read agentihooks msg inbox and handle every open item. "
        "When no work remains, run agentihooks swarm sw wait --inbox in a foreground tool call. "
        "It returns pending inbox work or times out after one minute; read the inbox and wait again. "
        "Keep the tool call active while waiting so new work resumes this turn without pane input. "
        "Use swarm wait --on checks, reply or task for a specific dependency."
    )


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

    fresh = render.render_codex("rb-role", force=True)

    assert not (fresh / "skills" / "deploy").exists()
    assert (out / "skills" / ".system" / "codex" / "SKILL.md").read_text() == "codex's own\n"


def test_codex_render_config_has_no_persona_and_only_profile_servers(world):
    from scripts.profiles import render

    home = world["home"]
    doc = tomllib.loads((render.render_codex("rb-role") / "config.toml").read_text())

    assert "developer_instructions" not in doc
    assert doc["sandbox_mode"] == "workspace-write"
    assert doc["approval_policy"] == "never"
    assert doc["features"]["hooks"] is True
    assert set(doc["mcp_servers"]) == {"agentihooks", "bundle-srv", "role-srv", "leaky-env"}
    assert doc["mcp_servers"]["role-srv"] == {"command": "role-server"}
    assert doc["mcp_servers"]["bundle-srv"] == {"command": "bundle-server"}
    skills = home / ".agents" / "skills"
    assert doc["skills"]["config"] == [
        {"path": str(skills / name / "SKILL.md"), "enabled": False}
        for name in ("bundle-skill", "other-skill", "role-skill")
    ]
    assert doc["project_doc_max_bytes"] == 65536
    assert "agentihooks" not in doc
    stamp = json.loads((render.rendered_root() / "rb-role" / "codex" / render.STAMP).read_text())
    assert stamp["render"] == render.stamp("rb-role")
    operator = (home / ".codex" / "config.toml").read_bytes()
    assert stamp["operator"] == hashlib.sha256(operator).hexdigest()
    assert render.render_codex("rb-role") is None


def test_codex_render_keeps_hooks_on_and_layer_servers_out(world):
    from scripts.profiles import render

    layer = "[features]\nhooks = false\n\n[mcp_servers.layer-srv]\ncommand = 'x'\n"
    _write(world["role"] / ".codex" / "config.overrides.toml", layer)

    doc = tomllib.loads((render.render_codex("rb-role") / "config.toml").read_text())

    assert doc["features"]["hooks"] is True
    assert "layer-srv" not in doc["mcp_servers"]


def test_codex_render_status_line_keeps_context_apart_from_cumulative_tokens(world):
    from scripts.profiles import render

    line = tomllib.loads((render.render_codex("rb-role") / "config.toml").read_text())["tui"]["status_line"]

    context = line.index("context-used")
    assert line[context + 1] == "context-window-size"
    assert not {"context-usage", "used-tokens"} & set(line)


def test_codex_render_keeps_a_profile_status_line(world):
    from scripts.profiles import render

    _write(world["role"] / ".codex" / "config.overrides.toml", '[tui]\nstatus_line = ["model", "used-tokens"]\n')

    doc = tomllib.loads((render.render_codex("rb-role") / "config.toml").read_text())

    assert doc["tui"]["status_line"] == ["model", "used-tokens"]


READS = ["lf-swarm_traces_by_tag", "lf-swarm_session_timeline", "lf-swarm_error_latency_summary"]
GW_HEADERS = {"Authorization": "Bearer ${GW_KEY}", "x-mcp-servers": "lf", "X-Scope": "${GW_SCOPE}"}


def _declare(world, **servers) -> None:
    path = world["role"] / ".claude" / ".mcp.json"
    doc = json.loads(path.read_text())
    doc["mcpServers"].update(servers)
    path.write_text(json.dumps(doc))


def _gateway(**extra) -> dict:
    return {"type": "http", "url": "https://gw.example/mcp/", "headers": dict(GW_HEADERS), **extra}


def _mounts(name: str, target: str) -> dict:
    from scripts.profiles import connectors, render

    return json.loads(connectors.path(name, target, render.rendered_root()).read_text())


@pytest.fixture
def advertised(monkeypatch):
    from scripts.profiles import connectors

    calls = []

    def fake(url, headers):
        calls.append((url, headers))
        return [*READS, "lf-trace_list", "lf-prompts_get"]

    monkeypatch.setattr(connectors, "advertised", fake)
    monkeypatch.setenv("GW_KEY", "k-test")
    monkeypatch.setenv("GW_SCOPE", "s-test")
    return calls


def test_codex_render_mounts_the_declared_connector_not_the_installed_entry(world, advertised):
    from scripts.profiles import render

    installed = (world["home"] / ".codex" / "config.toml").read_text()
    _write(world["home"] / ".codex" / "config.toml", installed + "\n[mcp_servers.gw]\nurl = 'https://old.example'\n")
    _declare(world, gw=_gateway(enabled_tools=READS))

    doc = tomllib.loads((render.render_codex("rb-role") / "config.toml").read_text())

    assert doc["mcp_servers"]["gw"] == {
        "url": "https://gw.example/mcp/",
        "bearer_token_env_var": "GW_KEY",
        "http_headers": {"x-mcp-servers": "lf"},
        "env_http_headers": {"X-Scope": "GW_SCOPE"},
        "enabled_tools": READS,
    }
    assert advertised == [
        ("https://gw.example/mcp/", {**GW_HEADERS, "Authorization": "Bearer k-test", "X-Scope": "s-test"})
    ]


def test_codex_render_names_each_declared_server_it_cannot_mount(world, advertised, capsys):
    from scripts.profiles import render

    _declare(world, gw=_gateway(enabled_tools=READS), old={"type": "sse", "url": "http://x/sse"})

    render.render_codex("rb-role")

    mounts = _mounts("rb-role", "codex")
    assert mounts["gw"] == {"mounted": True, "enabled_tools": READS}
    assert mounts["role-srv"] == {"mounted": True}
    assert mounts["old"] == {"mounted": False, "reason": "codex has no SSE transport"}
    reason = "credential-shaped literal in url, command or args"
    assert mounts["leaky-arg"] == {"mounted": False, "reason": reason}
    printed = " ".join(capsys.readouterr().out.split())
    assert f"[!!] MCP 'leaky-arg' is not mounted for rb-role (codex): {reason}" in printed


def test_claude_render_denies_every_advertised_tool_outside_the_allowlist(world, advertised):
    from scripts.profiles import render

    _declare(world, gw=_gateway(enabled_tools=READS, disabled_tools=["lf-x"]))

    out = render.render_claude("rb-role")

    entry = json.loads((out / ".claude.json").read_text())["mcpServers"]["gw"]
    assert entry == {"type": "http", "url": "https://gw.example/mcp/", "headers": GW_HEADERS}
    deny = json.loads((out / "settings.json").read_text())["permissions"]["deny"]
    assert [rule for rule in deny if rule.startswith("mcp__gw__")] == [
        "mcp__gw__lf-trace_list",
        "mcp__gw__lf-prompts_get",
        "mcp__gw__lf-x",
    ]
    assert _mounts("rb-role", "claude")["gw"] == {"mounted": True, "enabled_tools": READS}


def test_claude_render_denies_a_disabled_tool_without_listing(world, advertised):
    from scripts.profiles import render

    _declare(world, gw=_gateway(disabled_tools=["lf-trace_list"]))

    out = render.render_claude("rb-role")

    deny = json.loads((out / "settings.json").read_text())["permissions"]["deny"]
    assert "mcp__gw__lf-trace_list" in deny
    assert advertised == []


def test_claude_render_keeps_the_profile_deny_rules_ahead_of_connector_denies(world, advertised):
    from scripts.profiles import render

    overrides = world["role"] / ".claude" / "settings.overrides.json"
    doc = json.loads(overrides.read_text())
    doc["permissions"] = {"deny": ["Bash(rm:*)"]}
    overrides.write_text(json.dumps(doc))
    _declare(world, gw=_gateway(disabled_tools=["lf-x"]))

    out = render.render_claude("rb-role")

    assert json.loads((out / "settings.json").read_text())["permissions"]["deny"] == ["Bash(rm:*)", "mcp__gw__lf-x"]


@pytest.mark.parametrize(
    ("spec", "env", "reason"),
    [
        (
            {"command": "srv", "enabled_tools": READS},
            {},
            "Claude has no native tool allowlist; only an http server's tools can be listed",
        ),
        (_gateway(enabled_tools=READS), {"FAIL": "1"}, "tool listing failed: ConnectionError: refused"),
    ],
)
def test_claude_render_leaves_an_unlistable_allowlisted_server_unmounted(
    world, advertised, monkeypatch, capsys, spec, env, reason
):
    from scripts.profiles import connectors, render

    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key)
    if "FAIL" in env:
        monkeypatch.setattr(
            connectors, "advertised", lambda url, headers: (_ for _ in ()).throw(ConnectionError("refused"))
        )
    _declare(world, gw=spec)

    out = render.render_claude("rb-role")

    assert "gw" not in json.loads((out / ".claude.json").read_text())["mcpServers"]
    assert _mounts("rb-role", "claude")["gw"] == {"mounted": False, "reason": reason}
    printed = " ".join(capsys.readouterr().out.split())
    assert f"[!!] MCP 'gw' is not mounted for rb-role (claude): {reason}" in printed


def test_codex_render_sizes_the_doc_cap_to_a_large_persona(world):
    from scripts.profiles import render

    _write(world["role"] / ".claude" / "rules" / "big.md", "x" * 100_000 + "\n")

    out = render.render_codex("rb-role")

    persona = len((render.rendered_root() / "rb-role" / "claude" / "CLAUDE.md").read_bytes())
    assert tomllib.loads((out / "config.toml").read_text())["project_doc_max_bytes"] == int(persona * 1.25)


def test_codex_render_force_rerenders_the_claude_profile(world):
    from scripts.profiles import render

    out = render.render_codex("rb-role")
    persona = out.parent / "claude" / "CLAUDE.md"
    persona.write_text("stale\n")

    fresh = render.render_codex("rb-role", force=True)
    assert fresh != out
    assert "ROLE PERSONA MARKER" in (fresh / "AGENTS.md").read_text()


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

    fresh = render.render_codex("rb-role")
    assert fresh not in (None, out)
    assert fresh == render.profile_dir("rb-role") / "codex"
    assert tomllib.loads((fresh / "config.toml").read_text())["model"] == "gpt-op"


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

    assert set(doc["mcp_servers"]) == {"agentihooks", "bundle-srv", "role-srv", "leaky-env"}
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


def test_codex_render_leaves_an_old_plain_sources_file_of_an_earlier_layout(world):
    from scripts.profiles import render

    manifest = render.sources.path("rb-role", "codex", render.rendered_root())
    old = _write(manifest, '{"old": true}\n')

    out = render.render_codex("rb-role")

    linked = render.sources.path(out.parent.name, "codex", out.parent.parent)
    assert os.readlink(linked) == str(render.sources.path(out.parent.name, "claude", out.parent.parent))
    assert old.read_text() == '{"old": true}\n'


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
def test_operator_plugins_redo_only_operator_homes(world, target):
    from scripts.profiles import render

    _write(world["bundle"] / "profiles" / "engineer" / "profile.yml", "name: engineer\nextends: [rb-base]\n")
    settings = world["home"] / ".claude" / "settings.json"
    _write(settings, json.dumps({"enabledPlugins": {"mine@m": True}}))
    assert render.render(target, "rb-role") is not None
    assert render.render(target, "engineer") is not None

    _write(settings, json.dumps({"enabledPlugins": {"mine@m": True, "later@m": True}}))
    assert render.render(target, "engineer") is None
    assert render.render(target, "rb-role") is not None
    assert render.render(target, "rb-role") is None
    enabled = json.loads((render.profile_dir("rb-role") / "claude" / "settings.json").read_text())["enabledPlugins"]
    assert enabled == {"mine@m": True, "later@m": True}


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_stamp_redoes_render_when_agentihooks_base_settings_change(world, target, tmp_path, monkeypatch):
    from scripts.profiles import render

    install = world["install"]
    profiles = tmp_path / "agentihooks-profiles"
    shutil.copytree(install.PROFILES_DIR / "_base", profiles / "_base")
    monkeypatch.setattr(install, "PROFILES_DIR", profiles)
    assert render.render(target, "rb-role") is not None
    assert render.render(target, "rb-role") is None

    base = profiles / "_base" / install._NATIVE_BASE_NAME[target]
    base.write_text(base.read_text() + "\n")
    assert render.render(target, "rb-role") is not None


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_stamp_redoes_render_when_a_package_rule_changes(world, target, tmp_path, monkeypatch):
    from scripts.profiles import binding, render

    install = world["install"]
    package = tmp_path / "agentihooks-package"
    shutil.copytree(install.PACKAGE_FEATURES_DIR, package)
    monkeypatch.setattr(install, "PACKAGE_FEATURES_DIR", package)
    assert render.render(target, "rb-role") is not None
    assert render.render(target, "rb-role") is None

    _write(package / "rules" / "fresh-rule.md", "FRESH PACKAGE RULE MARKER\n")
    out = render.render(target, "rb-role")
    assert out is not None
    assert "FRESH PACKAGE RULE MARKER" in (out / binding.PERSONAS[target]).read_text()
    assert render.render(target, "rb-role") is None

    _write(package / "skills" / "fresh-skill" / "__pycache__" / "run.cpython-313.pyc", "bytecode\n")
    assert render.render(target, "rb-role") is None

    (package / "rules" / "fresh-rule.md").rename(package / "rules" / "renamed-rule.md")
    assert render.render(target, "rb-role") is not None


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_stamp_redoes_render_when_a_linked_profile_changes(world, named_brain, target):
    from scripts.profiles import binding, render

    assert render.render(target, "rb-role") is not None
    assert render.render(target, "rb-role") is None

    _write(named_brain / ".claude" / "skills" / "brain-memory" / "SKILL.md", "---\nname: brain-memory\n---\n")
    out = render.render(target, "rb-role")
    assert out is not None
    assert (out / "skills" / "brain-memory" / "SKILL.md").is_file()
    assert render.render(target, "rb-role") is None

    _write(named_brain / "CLAUDE.md", "BRAIN PERSONA MARKER\n")
    out = render.render(target, "rb-role")
    assert out is not None
    assert "BRAIN PERSONA MARKER" in (out / binding.PERSONAS[target]).read_text()


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_a_running_agent_stays_valid_after_a_package_rule_rerenders_its_home(world, target, tmp_path, monkeypatch):
    from scripts.profiles import binding, render

    install = world["install"]
    package = tmp_path / "agentihooks-package"
    shutil.copytree(install.PACKAGE_FEATURES_DIR, package)
    monkeypatch.setattr(install, "PACKAGE_FEATURES_DIR", package)
    home = render.render(target, "rb-role")
    report = tmp_path / "report.json"
    binding.request(report, "rb-role", target, home)
    env = {"AGENTIHOOKS_PROFILE": "rb-role", binding.HOMES[target]: str(home), binding.REPORT: str(report)}
    monkeypatch.setattr(binding, "process", lambda: (123, target, env, "default"))
    launched = binding.validate(binding.inspect(home, "rb-role", target)["canary"])

    _write(package / "rules" / "fresh-rule.md", "FRESH PACKAGE RULE MARKER\n")
    rerendered = render.render(target, "rb-role")
    assert rerendered not in (None, home)
    fresh = binding.inspect(rerendered, "rb-role", target)
    assert fresh["canary"] != launched["canary"]
    assert binding.inspect(home, "rb-role", target)["canary"] == launched["canary"]

    assert binding.validate(launched["canary"])["persona"] == launched["persona"]


def test_stamp_names_the_chain_role_defaults(world, monkeypatch):
    from scripts.profiles import plugins, render

    _write(world["bundle"] / "profiles" / "master" / "profile.yml", "name: master\nextends: [rb-role]\n")
    assert render.stamp("rb-role")["enabled_plugins"] == {}
    assert render.stamp("master")["enabled_plugins"] == {PLAYWRIGHT: True}
    assert render.render("claude", "master") is not None

    monkeypatch.setitem(plugins.ROLE_PLUGINS, "master", ("other@m",))
    assert render.stamp("master")["enabled_plugins"] == {"other@m": True}
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

    assert not (home / "skills" / "new-skill").exists()
    assert (render.profile_dir("rb-role") / "claude" / "skills" / "new-skill").is_symlink()
    lines = capsys.readouterr().out.splitlines()
    assert f"{install._DIM}[--] Profile home gone kept as it was: Profile 'gone' not found{install._RESET}" in lines
    assert f"{install._GREEN}[OK]{install._RESET} Re-rendered the rb-role profile home" in lines


@pytest.mark.parametrize("target", ["claude", "codex"])
@pytest.mark.parametrize("validated", [False, True])
def test_running_profile_validates_its_launched_persona_after_other_profile_render(
    world, monkeypatch, target, validated
):
    from scripts import init_agent
    from scripts.profiles import binding, render

    home = render.render(target, "rb-role")
    launched = binding.inspect(home, "rb-role", target)
    env = {"AGENTIHOOKS_PROFILE": "rb-role", binding.HOMES[target]: str(home), "XDG_RUNTIME_DIR": str(world["home"])}
    init_agent._binding_request(SimpleNamespace(profile="rb-role"), target, "", env, [])
    report = Path(env[binding.REPORT])
    monkeypatch.setattr(binding, "process", lambda: (123, target, env, "default"))
    if validated:
        assert binding.validate(launched["canary"])["persona"] == launched["persona"]

    _write(world["bundle"] / "profiles" / "rb-other" / "CLAUDE.md", "OTHER PERSONA UPDATED\n")
    _commit(world["bundle"], "change other profile")
    hooks_python = Path(sys.executable)
    monkeypatch.setattr(world["install"], "_resolve_hooks_python", lambda: hooks_python)
    interpreter = world["home"] / "init-python"
    interpreter.symlink_to(sys.executable)
    monkeypatch.setattr(binding.sys, "executable", str(interpreter))
    render.render("claude", "rb-other")
    assert binding.digest(home / binding.PERSONAS[target]) == launched["persona"]

    world["install"]._rerender_profile_homes("claude")
    rewritten = binding.digest(home / binding.PERSONAS[target])
    print(f"{target}: before={launched['persona']} after={rewritten}")
    result = binding.validate(launched["canary"])
    assert result["persona"] == launched["persona"]
    assert result["sources"] == launched["sources"]
    assert result["revisions"] == launched["revisions"]

    _write(world["role"] / "CLAUDE.md", "UPDATED ENGINEER PERSONA\n")
    _commit(world["bundle"], "change launched profile")
    fresh = render.render(target, "rb-role", force=True)
    assert binding.inspect(home, "rb-role", target) == launched
    refreshed = binding.inspect(fresh, "rb-role", target)
    assert refreshed["canary"] != launched["canary"]
    result = binding.validate(launched["canary"])
    assert result["persona"] == launched["persona"]
    assert result["source_blobs"] == launched["source_blobs"]

    env[binding.HOMES[target]] = str(fresh)
    binding.request(report, "rb-role", target)
    with pytest.raises(ValueError, match="canary mismatch"):
        binding.validate(launched["canary"])
    assert binding.validate(refreshed["canary"])["persona"] == refreshed["persona"]


def test_stamp_names_bundle_commit_and_chain(world):
    from scripts.profiles import render

    head = _git(world["bundle"], "rev-parse", "HEAD").strip()
    chain = ["rb-base", "rb-kit", "rb-role"]
    base = render._base_digest()
    profiles = render._profiles_digest(render._chain("rb-role"))
    assert render.stamp("rb-role") == {
        "bundle_commit": head,
        "base": base,
        "profiles": profiles,
        "chain": chain,
        "overlays": [],
        "enabled_plugins": {},
        "corrections": "",
    }
    assert render._stamp(None, []) == {
        "bundle_commit": "",
        "base": base,
        "profiles": render._profiles_digest([]),
        "chain": [],
        "overlays": [],
        "enabled_plugins": {},
        "corrections": "",
    }
    assert render._roots(None, [("rb-role", world["role"])]) == [world["role"]]


def test_render_refuses_other_targets(world):
    from scripts.profiles import render

    with pytest.raises(ValueError, match="^gemini per-run profiles are not supported$"):
        render.render("gemini", "rb-role")


def test_cli_renders_and_refuses(world, capsys):
    from scripts.profiles import render

    out = Path.home() / ".agentihooks" / "profiles" / "rb-role" / "claude"
    assert render.main(["render", "rb-role"]) == 0
    assert capsys.readouterr().out.endswith(f"\nRendered rb-role (claude) → {out.resolve()}\n")
    assert render.main(["render", "rb-role", "--target", "claude"]) == 0
    assert capsys.readouterr().out == "rb-role (claude) is up to date\n"
    assert render.main(["render", "rb-role", "--force"]) == 0
    assert capsys.readouterr().out.endswith(f"\nRendered rb-role (claude) → {out.resolve()}\n")
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
    assert capsys.readouterr().err.startswith("usage: agentihooks profile [-h] {render,measure,validate,binding}")
    with pytest.raises(SystemExit):
        render.main(["--help"])
    assert re.search(
        r"(?<!\S)render Render a profile into its own home for one harness(?!\S)", _flat(capsys.readouterr().out)
    )


def test_agentihooks_profile_dispatches_to_render(world, copilot_gateway, monkeypatch, capsys):
    from scripts import install
    from scripts.profiles import render

    monkeypatch.setattr("sys.argv", ["agentihooks", "profile", "render", "rb-role", "--target", "copilot"])
    with pytest.raises(SystemExit) as exc:
        install.main()
    assert exc.value.code == 0
    assert ROLE_TOOLSET in (render.rendered_root() / "rb-role" / "copilot" / "mcp-config.json").read_text()


def test_agentihooks_help_lists_profile(monkeypatch, capsys):
    from scripts import install

    monkeypatch.setattr("sys.argv", ["agentihooks", "--help"])
    with pytest.raises(SystemExit):
        install.main()
    line = r"(?<!\S)profile Render a profile into its own home: render NAME --target claude\|codex\|copilot \[--force\] \[--out DIR \[--bundle DIR\]\](?!\S)"
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


@pytest.fixture
def brain_overlay(world, tmp_path):
    brain = tmp_path / "kernel" / "profiles" / "rb-brain"
    _write(brain / "profile.yml", "name: rb-brain\n")
    _write(brain / "CLAUDE.md", "BRAIN USAGE MARKER\n")
    _write(brain / ".claude" / ".mcp.json", json.dumps({"mcpServers": {"rb-brain-srv": {"type": "http", "url": "u"}}}))
    _write(brain / ".claude" / "skills" / "brain-skill" / "SKILL.md", "---\nname: brain-skill\n---\n")
    _write(brain / ".claude" / "rules" / "brain-rule.md", "BRAIN RULE MARKER\n")
    _write(brain / ".claude" / "settings.overrides.json", json.dumps({"env": {"BRAIN_FLAG": "1"}}))
    install = world["install"]
    state = install._load_state()
    state["linked_profiles"] = [{"name": "rb-brain", "path": str(brain)}]
    install._save_state(state)
    profiles = world["bundle"] / "profiles"
    _write(profiles / "rb-base" / "profile.yml", "name: rb-base\nallowedOverlays: [rb-router, rb-brain]\n")
    _write(profiles / "rb-op" / "profile.yml", "name: rb-op\nextends: [rb-base]\nallowedOverlays: [rb-brain]\n")
    _write(profiles / "rb-op" / "CLAUDE.md", "OPERATOR PERSONA MARKER\n")
    return brain


@pytest.mark.parametrize("name", ["rb-role", "rb-op"])
@pytest.mark.parametrize("target", ["claude", "codex"])
def test_render_layers_each_declared_overlay_like_a_profile(world, brain_overlay, name, target):
    from scripts.profiles import render

    render.render(target, name)
    out = world["home"] / ".agentihooks" / "profiles" / name / "claude"

    persona = (out / "CLAUDE.md").read_text()
    assert persona.count("BRAIN USAGE MARKER") == 1 and "BRAIN RULE MARKER" in persona
    assert "**rb-brain**" in persona and f"You are **{name}**" in persona
    assert (out / "skills" / "brain-skill").is_symlink()
    assert json.loads((out / "settings.json").read_text())["env"]["BRAIN_FLAG"] == "1"
    assert "rb-brain-srv" in json.loads((out / ".claude.json").read_text())["mcpServers"]
    chain = json.loads((out / render.STAMP).read_text())["chain"]
    assert chain[-1] == "rb-brain" and chain.count("rb-brain") == 1 and "rb-router" not in chain
    assert json.loads((out / render.STAMP).read_text())["overlays"] == ["rb-brain"]
    if target == "codex":
        config = tomllib.loads((out.parent / "codex" / "config.toml").read_text())
        assert "rb-brain-srv" in config["mcp_servers"]


@pytest.mark.parametrize("name,declared", [("rb-role", ["rb-brain"]), ("rb-op", ["rb-brain"]), ("rb-other", [])])
def test_declared_names_each_resolvable_overlay_once(world, brain_overlay, name, declared):
    from scripts.profiles import render

    assert render.declared(name) == declared


def test_render_skips_an_overlay_no_profile_declares(world, brain_overlay):
    from scripts.profiles import render

    out = render.render_claude("rb-other")

    assert "BRAIN USAGE MARKER" not in (out / "CLAUDE.md").read_text()
    assert json.loads((out / render.STAMP).read_text())["chain"] == ["rb-other"]
    assert json.loads((out / render.STAMP).read_text())["overlays"] == []


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
    assert capfd.readouterr().out.endswith(f"Rendered rb-role (claude) → {home.resolve()}\n")
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
    assert capfd.readouterr().out.endswith(f"Rendered rb-role (claude) → {home.resolve()}\n")
    assert render.main(["render", "rb-role", "--out", str(out)]) == 0
    assert capfd.readouterr().out.endswith("rb-role (claude) is up to date\n")
    assert render.main(["render", "rb-role", "--out", str(out), "--force"]) == 0
    assert capfd.readouterr().out.endswith(f"Rendered rb-role (claude) → {home.resolve()}\n")
    assert not (render.rendered_root() / "rb-role").exists()


def _target_stamp(profiles: Path, target: str) -> dict:
    from scripts.profiles import render

    stamp = json.loads((profiles / "rb-role" / target / render.STAMP).read_text())
    return stamp["render"] if target == "codex" else stamp


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_scratch_render_keeps_the_linked_overlays_of_the_live_render(world, brain_overlay, tmp_path, target):
    from scripts.profiles import render

    install = world["install"]
    render.render(target, "rb-role")
    live = _target_stamp(render.rendered_root(), target)
    state = install.STATE_JSON.read_bytes()
    before = _tree_hashes(install.AGENTIHOOKS_STATE_DIR, tmp_path / "none")
    out = tmp_path / "scratch-home"

    assert render.main(["render", "rb-role", "--target", target, "--out", str(out)]) == 0

    scratch = _target_stamp(out / "profiles", target)
    assert live["overlays"] == scratch["overlays"] == ["rb-brain"]
    assert scratch["chain"] == live["chain"]
    assert install.STATE_JSON.read_bytes() == state
    assert _tree_hashes(install.AGENTIHOOKS_STATE_DIR, tmp_path / "none") == before
    assert json.loads((out / "state.json").read_text())["linked_profiles"] == [
        {"name": "rb-brain", "path": str(brain_overlay)}
    ]


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_scratch_render_replaces_a_reused_homes_linked_record_and_keeps_its_state(
    world, brain_overlay, tmp_path, target
):
    from scripts.profiles import render

    out = tmp_path / "scratch-home"
    stale = {"herdr": {"pane": "p1"}, "linked_profiles": [{"name": "rb-gone", "path": "/nowhere"}]}
    _write(out / "state.json", json.dumps(stale))

    assert render.main(["render", "rb-role", "--target", target, "--out", str(out)]) == 0

    assert json.loads((out / "state.json").read_text()) == {
        "herdr": {"pane": "p1"},
        "linked_profiles": [{"name": "rb-brain", "path": str(brain_overlay)}],
    }
    assert _target_stamp(out / "profiles", target)["overlays"] == ["rb-brain"]


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


@pytest.mark.parametrize(
    "entry",
    [
        ["-m", "scripts.profiles.render"],
        ["-c", "import sys; from scripts.install import main; sys.exit(main())", "profile"],
    ],
    ids=["module", "command"],
)
def test_scratch_render_works_from_both_entry_paths(world, tmp_path, entry):
    from scripts.profiles import render

    bundle, out = _scratch_bundle(world, tmp_path), tmp_path / "scratch-home"
    argv = [sys.executable, *entry, "render", "rb-role", "--out", str(out), "--bundle", str(bundle)]
    repo = Path(render.__file__).resolve().parents[2]

    done = subprocess.run(argv, cwd=repo, capture_output=True, text=True)

    home = out / "profiles" / "rb-role" / "claude"
    assert (done.returncode, done.stderr) == (0, "")
    assert done.stdout.endswith(f"Rendered rb-role (claude) → {home.resolve()}\n")
    assert "SCRATCH PERSONA MARKER" in (home / "CLAUDE.md").read_text()
    assert not (render.rendered_root() / "rb-role").exists()


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


def _home_files(home: Path) -> dict[str, bytes]:
    names = ("settings.json", "CLAUDE.md", ".claude.json", "config.toml", "AGENTS.md", ".profile-binding.json")
    found = {name: (home / name).read_bytes() for name in names if (home / name).is_file()}
    manifest = home.parent / f"{home.name}.sources.json"
    return {**found, "sources": manifest.read_bytes()}


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_a_new_render_stamp_launches_into_a_fresh_profile_home(world, monkeypatch, target):
    from scripts import select_profile
    from scripts.profiles import render

    monkeypatch.setattr(render.homes, "live_homes", lambda: [])
    first = render.render(target, "rb-role")
    _commit(world["bundle"], "stamp changes")

    second = render.render(target, "rb-role")

    assert second is not None and second.resolve() != first.resolve()
    digest = hashlib.sha256(json.dumps(render.stamp("rb-role"), sort_keys=True).encode()).hexdigest()[:12]
    assert second.name == target and second.parent.name.startswith(f"{digest}-")
    env, _ = select_profile.prepare("rb-role", target, "", "", [], {})
    assert env[render.binding.HOMES[target]] == str(second.resolve())
    assert render.render(target, "rb-role") is None


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_a_home_live_sessions_run_on_is_never_rewritten(world, monkeypatch, target):
    from scripts.profiles import render

    first = render.render(target, "rb-role")
    monkeypatch.setattr(render.homes, "live_homes", lambda: [first.resolve()])
    before = _home_files(first)

    _commit(world["bundle"], "stamp changes")
    render.render(target, "rb-role")
    render.render(target, "rb-role", force=True)
    _write(world["role"] / "CLAUDE.md", "UPDATED ROLE PERSONA\n")
    render.render(target, "rb-role")

    assert _home_files(first) == before
    assert render.binding.inspect(first, "rb-role", target)["persona"] == render.binding.digest(
        first / render.binding.PERSONAS[target]
    )


@pytest.mark.parametrize("target", ["codex", "claude"])
def test_a_running_session_keeps_passing_its_check_after_a_bundle_content_render(world, monkeypatch, tmp_path, target):
    from scripts.profiles import binding, render

    home = render.render(target, "rb-role")
    monkeypatch.setattr(render.homes, "live_homes", lambda: [home.resolve()])
    report = tmp_path / "report.json"
    binding.request(report, "rb-role", target, home)
    env = {"AGENTIHOOKS_PROFILE": "rb-role", binding.HOMES[target]: str(home), binding.REPORT: str(report)}
    monkeypatch.setattr(binding, "process", lambda: (123, target, env, "default"))
    launched = binding.validate(binding.inspect(home, "rb-role", target)["canary"])

    _write(world["bundle"] / ".claude" / "CLAUDE.md", "BUNDLE DIRECTIVE MARKER\nNEW MANIFESTO LINE\n")
    _commit(world["bundle"], "manifesto change")
    fresh = render.render(target, "rb-role")

    assert fresh not in (None, home)
    assert "NEW MANIFESTO LINE" in (fresh / binding.PERSONAS[target]).read_text()
    assert binding.inspect(home, "rb-role", target)["persona"] == launched["persona"]
    assert binding.validate(launched["canary"])["persona"] == launched["persona"]


def test_old_homes_go_once_no_live_session_uses_them(world, monkeypatch):
    from scripts.profiles import render

    monkeypatch.setattr(render.homes, "GRACE_SECONDS", 0)
    legacy = render.rendered_root() / "rb-role"
    _write(legacy / "claude" / "settings.json", "{}")
    live = [(legacy / "claude").resolve()]
    monkeypatch.setattr(render.homes, "live_homes", lambda: live)
    first = render.render_claude("rb-role")
    live.append(first.resolve())
    _commit(world["bundle"], "two")
    second = render.render_claude("rb-role")

    assert (legacy / "claude" / "settings.json").is_file() and first.is_dir()

    live.clear()
    _commit(world["bundle"], "three")
    third = render.render_claude("rb-role")

    assert not first.exists() and not second.exists() and third.is_dir()
    assert (render.rendered_root() / "rb-role" / "claude").resolve() == third.resolve()
    assert render.rendered_profiles("claude") == ["rb-role"]


def test_a_superseded_home_waits_out_the_launch_grace(world, monkeypatch):
    from scripts.profiles import render

    monkeypatch.setattr(render.homes, "live_homes", lambda: [])
    first = render.render_claude("rb-role")
    render.render_claude("rb-role", force=True)

    assert first.is_dir()


@pytest.fixture
def overlays(world, tmp_path, monkeypatch):
    from hooks.context import profile_chain

    roles = tmp_path / "package-roles"
    monkeypatch.setattr(profile_chain, "PACKAGE_ROLES", roles)
    _write(roles / "engineer" / "profile.yml", "name: engineer\n")
    profiles = world["bundle"] / "profiles"
    _write(profiles / "rb-eng" / "profile.yml", "name: rb-eng\nextends: [package:engineer]\n")
    for name, wears in (("ov-a", "engineer"), ("ov-b", "engineer, qa"), ("ov-c", "engineer"), ("ov-d", "engineer")):
        _write(profiles / name / "profile.yml", f"name: {name}\nkind: overlay\nwears: [{wears}]\n")
        _write(profiles / name / ".claude" / "rules" / f"{name}.md", f"{name.upper()} RULE MARKER\n")
    _write(profiles / "ov-plan" / "profile.yml", "name: ov-plan\nkind: overlay\nwears: [planner]\n")
    _commit(world["bundle"], "overlays")
    return profiles


def test_overlays_follow_the_role_chain_and_the_always_on_overlays(world, overlays, named_brain):
    from scripts.profiles import render

    manifest = overlays / "rb-eng" / "profile.yml"
    manifest.write_text(manifest.read_text() + "allowedOverlays: [brain]\n")

    chain = [name for name, _ in render._chain("rb-eng", ["ov-b", "ov-a"])]

    assert chain == ["package:engineer", "rb-eng", "brain", "ov-a", "ov-b"]
    assert render._overlays(render._chain("rb-eng", ["ov-b", "ov-a"])) == ["brain", "ov-a", "ov-b"]
    assert render.stamp("rb-eng")["overlays"] == ["brain"]


def test_an_always_on_overlay_worn_again_joins_the_chain_once(world, overlays, named_brain):
    from scripts.profiles import render

    manifest = overlays / "rb-eng" / "profile.yml"
    manifest.write_text(manifest.read_text() + "allowedOverlays: [brain]\n")
    (named_brain / "profile.yml").write_text("name: brain\nkind: overlay\nwears: [engineer]\n")

    chain = [name for name, _ in render._chain("rb-eng", ["brain", "ov-a"])]

    assert chain == ["package:engineer", "rb-eng", "brain", "ov-a"]


def test_a_home_key_refuses_a_name_holding_its_separator(world):
    from scripts.profiles import render

    for name, worn in (("rb+x", []), ("rb", ["ov+a"])):
        with pytest.raises(ValueError) as refused:
            render.home_key(name, worn)
        assert str(refused.value) == f"a profile or overlay name cannot hold +: {', '.join([name, *worn])}"


def test_an_agent_wears_at_most_three_overlays(world, overlays):
    from scripts.profiles import render

    with pytest.raises(ValueError) as refused:
        render.render_claude("rb-eng", overlays=["ov-a", "ov-b", "ov-c", "ov-d"])

    assert str(refused.value) == "an agent wears at most 3 overlays; 4 were chosen: ov-a, ov-b, ov-c, ov-d"
    assert render.rendered_profiles("claude") == []
    assert render.render_claude("rb-eng", overlays=["ov-a", "ov-b", "ov-c", "ov-a"]).is_dir()


def test_an_overlay_that_does_not_wear_the_role_is_refused(world, overlays):
    from scripts.profiles import render

    for overlay in ("ov-plan", "rb-other"):
        with pytest.raises(ValueError) as refused:
            render.render_codex("rb-eng", overlays=[overlay])
        assert str(refused.value) == f"overlay {overlay} does not wear the engineer role"
    assert render.rendered_profiles("claude") == []


def test_each_overlay_set_renders_its_own_home(world, overlays):
    from scripts.profiles import render

    plain = render.render_claude("rb-eng")
    one = render.render_claude("rb-eng", overlays=["ov-a"])
    two = render.render_claude("rb-eng", overlays=["ov-b", "ov-a"])

    assert len({plain.parent, one.parent, two.parent}) == 3
    assert render.home_key("rb-eng", ["ov-b", "ov-a"]) == "rb-eng+ov-a+ov-b"
    assert render.home_key("rb-eng", []) == "rb-eng"
    assert render.split_key("rb-eng+ov-a+ov-b") == ("rb-eng", ["ov-a", "ov-b"])
    assert render.split_key("rb-eng") == ("rb-eng", [])
    assert render.profile_dir("rb-eng", ["ov-a", "ov-b"]) == two.parent
    assert render.profile_dir("rb-eng") == plain.parent
    assert render.render_claude("rb-eng", overlays=["ov-b", "ov-a"]) is None
    assert render.render_claude("rb-eng", overlays=["ov-a", "ov-b"]) is None
    assert json.loads((plain / render.STAMP).read_text())["overlays"] == []
    assert json.loads((two / render.STAMP).read_text())["overlays"] == ["ov-a", "ov-b"]
    assert "OV-A RULE MARKER" in (one / "CLAUDE.md").read_text()
    assert "OV-A RULE MARKER" not in (plain / "CLAUDE.md").read_text()
    assert "OV-B RULE MARKER" not in (one / "CLAUDE.md").read_text()
    assert render.binding.inspect(one, "rb-eng", "claude")["profile"] == "rb-eng"
    assert render.rendered_profiles("claude") == ["rb-eng", "rb-eng+ov-a", "rb-eng+ov-a+ov-b"]

    codex = render.render_codex("rb-eng", overlays=["ov-a"])

    assert codex.parent == render.profile_dir("rb-eng", ["ov-a"])
    assert json.loads((codex / render.STAMP).read_text())["render"]["overlays"] == ["ov-a"]
    assert render.render_codex("rb-eng", overlays=["ov-a"]) is None
    assert render.render("codex", "rb-eng", overlays=["ov-a"]) is None


def test_codex_renders_a_new_overlay_set_into_its_own_home(world, overlays):
    from scripts.profiles import render

    codex = render.render_codex("rb-eng", overlays=["ov-c"])

    assert codex.parent == render.profile_dir("rb-eng", ["ov-c"])
    assert render.profile_dir("rb-eng") is None
    assert json.loads((codex.parent / "claude" / render.STAMP).read_text())["overlays"] == ["ov-c"]


def test_codex_force_rerenders_the_claude_home_of_its_overlay_set(world, overlays):
    from scripts.profiles import render

    claude = render.render_claude("rb-eng", overlays=["ov-a"])

    codex = render.render_codex("rb-eng", force=True, overlays=["ov-a"])

    assert codex.parent != claude.parent
    assert codex.parent == render.profile_dir("rb-eng", ["ov-a"])


def test_a_stale_codex_overlay_home_rerenders_in_its_overlay_home(world, overlays):
    from scripts.profiles import render

    first = render.render_codex("rb-eng", overlays=["ov-a"])
    config = world["home"] / ".codex" / "config.toml"
    config.write_text(config.read_text() + 'model = "changed"\n')

    codex = render.render_codex("rb-eng", overlays=["ov-a"])

    assert codex.parent != first.parent
    assert codex.parent == render.profile_dir("rb-eng", ["ov-a"])
    assert render.profile_dir("rb-eng") is None
    assert json.loads((codex / render.STAMP).read_text())["render"]["overlays"] == ["ov-a"]


def test_init_and_rule_refresh_rerender_an_overlay_home_with_its_overlays(world, overlays, monkeypatch, capsys):
    from scripts.profiles import render
    from scripts.targets.claude_target import refresh_rules

    home = render.render_claude("rb-eng", overlays=["ov-a"])
    pair = render.render_claude("rb-eng", overlays=["ov-b", "ov-a"])

    world["install"]._rerender_profile_homes("claude")

    rerendered = render.profile_dir("rb-eng", ["ov-a"]) / "claude"
    assert rerendered != home
    assert json.loads((rerendered / render.STAMP).read_text())["overlays"] == ["ov-a"]
    repaired = render.profile_dir("rb-eng", ["ov-a", "ov-b"]) / "claude"
    assert repaired != pair
    assert json.loads((repaired / render.STAMP).read_text()) == json.loads((pair / render.STAMP).read_text())
    install = world["install"]
    assert f"{install._GREEN}[OK]{install._RESET} Re-rendered the rb-eng+ov-a profile home" in capsys.readouterr().out

    _write(overlays / "ov-a" / ".claude" / "rules" / "ov-a.md", "UPDATED OVERLAY RULE\n")
    refresh_rules(rerendered / "rules", rerendered / "CLAUDE.md", rerendered / "CLAUDE.local.md", False)

    fresh = render.profile_dir("rb-eng", ["ov-a"]) / "claude"
    assert fresh != rerendered
    assert "UPDATED OVERLAY RULE" in (fresh / "CLAUDE.md").read_text()


def test_cli_renders_the_overlays_named(world, overlays, capsys):
    from scripts.profiles import render

    assert render.main(["render", "rb-eng", "--overlay", "ov-b", "--overlay", "ov-a"]) == 0
    out = render.profile_dir("rb-eng", ["ov-a", "ov-b"]) / "claude"
    assert capsys.readouterr().out == f"Rendered rb-eng (claude) → {out}\n"
    assert json.loads((out / render.STAMP).read_text())["overlays"] == ["ov-a", "ov-b"]
    assert render.main(["render", "rb-eng", "--overlay", "ov-plan"]) == 1
    assert capsys.readouterr().err == "ERROR: overlay ov-plan does not wear the engineer role\n"
    with pytest.raises(SystemExit):
        render.main(["render", "--help"])
    assert re.search(r"--overlay OVERLAY\s+Wear this overlay; repeat for up to three", capsys.readouterr().out)


def test_an_overlay_render_refuses_a_bundle_at_another_commit_than_its_launch_recorded(world, overlays):
    from scripts.profiles import render

    recorded = _git(world["bundle"], "rev-parse", "HEAD").strip()
    _commit(world["bundle"], "moved")
    head = _git(world["bundle"], "rev-parse", "HEAD").strip()

    with pytest.raises(ValueError) as refused:
        render.render("claude", "rb-eng", overlays=["ov-a"], bundle_revision=recorded)

    assert str(refused.value) == (
        f"the launch recorded bundle commit {recorded}, but the bundle at {world['bundle']} is at commit {head}; "
        f"check out {recorded} in the bundle before this launch renders"
    )
    assert render.profile_dir("rb-eng", ["ov-a"]) is None


def test_an_overlay_render_refuses_a_bundle_with_uncommitted_changes(world, overlays):
    from scripts.profiles import render

    recorded = _git(world["bundle"], "rev-parse", "HEAD").strip()
    _write(overlays / "ov-a" / ".claude" / "rules" / "ov-a.md", "EDITED RULE\n")

    with pytest.raises(ValueError) as refused:
        render.render("codex", "rb-eng", overlays=["ov-a"], bundle_revision=recorded)

    assert str(refused.value) == (
        f"the launch recorded bundle commit {recorded}, but the bundle at {world['bundle']} has uncommitted changes; "
        f"check out {recorded} in the bundle before this launch renders"
    )
    assert render.profile_dir("rb-eng", ["ov-a"]) is None


def test_an_overlay_render_refuses_a_recorded_commit_without_a_linked_bundle(world, overlays, monkeypatch):
    from scripts.profiles import render

    monkeypatch.setattr(render, "_bundle", lambda: None)

    with pytest.raises(ValueError) as refused:
        render.render("claude", "rb-eng", overlays=["ov-a"], bundle_revision="abc123")

    assert str(refused.value) == "the launch recorded bundle commit abc123, but no bundle is linked"


def test_an_overlay_render_refuses_a_bundle_git_cannot_read(world, overlays, monkeypatch, tmp_path):
    from scripts.profiles import render

    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setattr(render, "_bundle", lambda: plain)

    with pytest.raises(ValueError) as refused:
        render.render("claude", "rb-eng", overlays=["ov-a"], bundle_revision="abc123")

    assert str(refused.value).startswith(
        f"the launch recorded bundle commit abc123, but git cannot read the bundle at {plain}: fatal: not a git repository"
    )


def test_an_overlay_render_refuses_a_bundle_git_does_not_answer(world, overlays, monkeypatch):
    from scripts.profiles import render

    def hang(argv, **kwargs):
        assert kwargs["timeout"] == 10
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(render.subprocess, "run", hang)

    with pytest.raises(ValueError) as refused:
        render.render("claude", "rb-eng", overlays=["ov-a"], bundle_revision="abc123")

    assert str(refused.value) == (
        f"the launch recorded bundle commit abc123, but git did not answer within 10 seconds for the bundle at "
        f"{world['bundle']}"
    )


def test_the_bundle_pin_reads_head_and_status_as_text_within_ten_seconds(world, overlays, monkeypatch):
    from scripts.profiles import render

    calls = []

    def run(argv, **kwargs):
        calls.append((argv[3:], kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="abc123\n" if argv[3] == "rev-parse" else "", stderr="")

    monkeypatch.setattr(render.subprocess, "run", run)

    render._pin(world["bundle"], "abc123")

    options = {"capture_output": True, "text": True, "timeout": 10}
    assert calls == [(["rev-parse", "HEAD"], options), (["status", "--porcelain"], options)]


def test_an_overlay_render_from_the_recorded_commit_renders_and_stamps_it(world, overlays):
    from scripts.profiles import render

    recorded = _git(world["bundle"], "rev-parse", "HEAD").strip()

    out = render.render("claude", "rb-eng", overlays=["ov-a"], bundle_revision=recorded)

    assert "OV-A RULE MARKER" in (out / "CLAUDE.md").read_text()
    assert json.loads((out / render.STAMP).read_text())["bundle_commit"] == recorded
    assert render.render("claude", "rb-eng", overlays=["ov-a"], bundle_revision=recorded) is None
